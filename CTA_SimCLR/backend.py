import os
import struct
import numpy as np
import pandas as pd
import tifffile
import traceback
from scipy.ndimage import gaussian_filter
from scipy.optimize import curve_fit
from scipy.signal import find_peaks, butter, sosfiltfilt, hilbert, correlate
from scipy.interpolate import interp1d
from scipy.sparse import lil_matrix, csr_matrix
from skimage.measure import block_reduce
from skimage.filters import threshold_otsu
from sklearn.preprocessing import StandardScaler
from qtpy.QtCore import QThread, Signal
import warnings

try:
    from aicsimageio import AICSImage
except Exception:
    AICSImage = None

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

warnings.filterwarnings('ignore')


# ---------------------------------------------------------------------------
# Olympus ETS reader
# ---------------------------------------------------------------------------

def _find_ets_files(vsi_path):
    """Return sorted list of .ets files in the VSI companion folder."""
    base = os.path.splitext(os.path.basename(vsi_path))[0]
    companion = os.path.join(os.path.dirname(vsi_path), f'_{base}_')
    if not os.path.isdir(companion):
        return []
    results = []
    for root, _dirs, files in os.walk(companion):
        for f in files:
            if f.lower().endswith('.ets'):
                results.append(os.path.join(root, f))
    return sorted(results)


def _read_ets(ets_path):
    """
    Read all frames from an Olympus ETS (Encoded Tile Sequence) file.

    Binary layout:
      0-63   SIS outer header  — dir_offset at [32], dir_count at [40]
      64-127 ETS sub-header    — pixeltype at [72], width at [92], height at [96]
      ...    frame data (sequential 1-MB blocks from ~offset 292)
      EOF-N  tile directory    — dir_count × 44-byte entries

    Each 44-byte entry: struct '<11I'
      (type, pad, pad, T_idx, pad, pad, pad, offset_lo, offset_hi, size, seq)

    Returns (T, H, W) numpy array.
    """
    file_size = os.path.getsize(ets_path)
    with open(ets_path, 'rb') as fh:
        hdr = fh.read(128)

    if hdr[:3] != b'SIS':
        raise ValueError(f"Not a valid ETS file (bad magic): {ets_path}")

    dir_offset = struct.unpack_from('<Q', hdr, 32)[0]
    dir_count  = struct.unpack_from('<Q', hdr, 40)[0]
    pixeltype  = struct.unpack_from('<I', hdr, 72)[0]
    width      = struct.unpack_from('<I', hdr, 92)[0]
    height     = struct.unpack_from('<I', hdr, 96)[0]

    with open(ets_path, 'rb') as fh:
        fh.seek(dir_offset)
        raw_dir = fh.read(file_size - dir_offset)

    entries = []
    for i in range(int(dir_count)):
        e      = struct.unpack_from('<11I', raw_dir, i * 44)
        t_idx  = e[3]
        offset = e[7] | (e[8] << 32)
        size   = e[9]
        entries.append((t_idx, offset, size))

    if not entries:
        raise ValueError(f"ETS file contains no tile entries: {ets_path}")

    frame_pixels = width * height
    sample_size  = entries[0][2]
    if sample_size == frame_pixels:
        dtype = np.uint8
    elif sample_size == frame_pixels * 2:
        dtype = np.uint16
    else:
        dtype = np.uint8

    entries_sorted = sorted(entries, key=lambda e: e[0])
    n_frames = len(entries_sorted)

    result = np.empty((n_frames, height, width), dtype=dtype)
    with open(ets_path, 'rb') as fh:
        for i, (_t, offset, size) in enumerate(entries_sorted):
            fh.seek(offset)
            raw = fh.read(size)
            result[i] = np.frombuffer(raw, dtype=dtype).reshape(height, width)

    return result


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_image(path):
    if path.lower().endswith('.vsi'):
        ets_files = _find_ets_files(path)
        if ets_files:
            ets_path = max(ets_files, key=os.path.getsize)
            return _read_ets(ets_path)
        if AICSImage is None:
            raise ImportError("aicsimageio not installed. Run: pip install aicsimageio")
        img = AICSImage(path)
        return img.get_image_data("TYX")

    with tifffile.TiffFile(path) as tif:
        data = tif.asarray()
        axes = ''
        try:
            axes = tif.series[0].axes.upper()
        except Exception:
            pass

    if data.ndim == 2:
        return data[np.newaxis, ...]

    if data.ndim == 3:
        return data

    if data.ndim == 4:
        if axes and 'C' in axes:
            c = axes.index('C')
            data = np.take(data, 0, axis=c)
        elif axes and 'T' in axes and 'Z' in axes:
            z = axes.index('Z')
            data = np.take(data, 0, axis=z)
        elif axes and len(axes) == 4:
            try:
                t_pos = axes.index('T')
                y_pos = axes.index('Y')
                x_pos = axes.index('X')
                other = [i for i in range(4) if i not in (t_pos, y_pos, x_pos)][0]
                data = np.take(data, 0, axis=other)
            except Exception:
                data = data[0]
        else:
            small_ax = int(np.argmin(data.shape))
            if data.shape[small_ax] <= 4:
                data = np.take(data, 0, axis=small_ax)
            else:
                data = data[0]
        return data if data.ndim == 3 else data[0]

    raise ValueError(
        f"Unsupported TIFF shape {data.shape}. Expected (T,Y,X), (T,C,Y,X), or similar."
    )


def read_file_timing(path):
    """
    Extract FPS from file metadata.
    Returns dict: fps (float or None), T (int or None), source (str or None).
    """
    result = {'fps': None, 'T': None, 'source': None}

    sidecar = os.path.splitext(path)[0] + '.fps'
    if os.path.isfile(sidecar):
        try:
            import json
            with open(sidecar) as fh:
                data = json.load(fh)
            fps_val = float(data.get('fps', 0))
            if fps_val > 0:
                result['fps']    = fps_val
                result['source'] = f"sidecar ({sidecar})"
                return result
        except Exception:
            pass

    if path.lower().endswith('.vsi'):
        try:
            ets_files = _find_ets_files(path)
            if ets_files:
                ets_path = max(ets_files, key=os.path.getsize)
                with open(ets_path, 'rb') as fh:
                    hdr = fh.read(48)
                result['T'] = int(struct.unpack_from('<Q', hdr, 40)[0])
        except Exception:
            pass
        return result

    if not path.lower().endswith(('.tif', '.tiff')):
        return result

    try:
        with tifffile.TiffFile(path) as tif:
            try:
                result['T'] = int(tif.series[0].shape[0])
            except Exception:
                pass

            ij = tif.imagej_metadata or {}
            software_tag = tif.pages[0].tags.get(305)
            written_by_tifffile = (software_tag and
                                   'tifffile' in str(software_tag.value).lower())
            fi = ij.get('finterval')
            if fi and float(fi) > 0:
                fi_f = float(fi)
                if fi_f == 1.0 and written_by_tifffile:
                    pass
                else:
                    result['fps']    = round(1.0 / fi_f, 4)
                    result['source'] = 'ImageJ metadata (finterval)'
                    return result
            fp = ij.get('fps')
            if fp and float(fp) > 0:
                result['fps']    = round(float(fp), 4)
                result['source'] = 'ImageJ metadata (fps)'
                return result

            if tif.is_ome:
                try:
                    import xml.etree.ElementTree as ET
                    ome_xml = tif.ome_metadata
                    root = ET.fromstring(ome_xml)
                    for pixels in root.iter():
                        if pixels.tag.endswith('Pixels'):
                            ti = pixels.get('TimeIncrement')
                            tu = pixels.get('TimeIncrementUnit', 's')
                            if ti:
                                ti = float(ti)
                                if tu in ('ms', 'millisecond', 'Milliseconds'):
                                    ti /= 1000.0
                                elif tu in ('min', 'Minutes'):
                                    ti *= 60.0
                                if ti > 0:
                                    result['fps']    = round(1.0 / ti, 4)
                                    result['source'] = 'OME-TIFF (TimeIncrement)'
                                    return result
                except Exception:
                    pass

            mm = getattr(tif, 'micromanager_metadata', None)
            if mm:
                try:
                    interval_ms = mm.get('Summary', {}).get('Interval_ms')
                    if interval_ms and float(interval_ms) > 0:
                        result['fps']    = round(1000.0 / float(interval_ms), 4)
                        result['source'] = 'MicroManager (Interval_ms)'
                        return result
                except Exception:
                    pass

            info_str = ij.get('Info', '')
            if info_str:
                try:
                    import re
                    vals  = {int(m.group(1)): float(m.group(2))
                             for m in re.finditer(r'^Value #(\d+)\s*=\s*([\d.eE+\-]+)',
                                                  info_str, re.MULTILINE)}
                    units = {int(m.group(1)): m.group(2).strip()
                             for m in re.finditer(r'^Units #(\d+)\s*=\s*([^\n]+)',
                                                  info_str, re.MULTILINE)}
                    ts_ms = [vals[i] for i in sorted(vals)
                             if '10^-3s^1' in units.get(i, '')]
                    if len(ts_ms) >= 2:
                        ts = np.array(ts_ms) / 1000.0
                        avg_interval = (ts[-1] - ts[0]) / (len(ts) - 1)
                        if avg_interval > 0:
                            result['fps']        = round(1.0 / avg_interval, 4)
                            result['source']     = 'Olympus cellSens (frame timestamps)'
                            result['timestamps'] = ts
                            return result
                except Exception:
                    pass
    except Exception:
        pass

    return result


def save_fps_sidecar(tiff_path, fps):
    """Save user-confirmed FPS next to the TIFF so it auto-loads on next open."""
    import json
    sidecar = os.path.splitext(tiff_path)[0] + '.fps'
    with open(sidecar, 'w') as fh:
        json.dump({'fps': fps, 'path': os.path.basename(tiff_path)}, fh)


def convert_single_vsi(input_path):
    """
    Converts VSI to ImageJ TIFF. Reads frames from the ETS companion file when present.
    FPS metadata is not available in Olympus VSI; defaults to finterval=1.0 s.
    """
    try:
        ets_files = _find_ets_files(input_path)
        if ets_files:
            ets_path = max(ets_files, key=os.path.getsize)
            data = _read_ets(ets_path)
        elif AICSImage is not None:
            img = AICSImage(input_path)
            reader_dims = img.dims.order
            if "C" in reader_dims:
                data = img.get_image_data("TCYX")
                data = data[:, 0, :, :]
            else:
                data = img.get_image_data("TYX")
        else:
            return False, "No ETS companion file found and aicsimageio is not installed."

        save_path = os.path.splitext(input_path)[0] + ".tif"
        tifffile.imwrite(
            save_path,
            data,
            photometric='minisblack',
            metadata={'axes': 'TYX'},
            imagej=True,
        )
        return True, f"Saved {data.shape[0]} frames to {os.path.basename(save_path)}"

    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Photobleaching helpers
# ---------------------------------------------------------------------------

def _bleach_envelope(sig, n_pts):
    n   = len(sig)
    win = max(n // 8, 3)
    return np.array([
        np.percentile(sig[max(0, i - win // 2): i + win // 2 + 1], 10)
        for i in range(n)
    ])


def _single_exp(t, a, tau, c):
    tau = max(tau, 1e-5)
    return a * np.exp(-np.clip(t / tau, 0, 700)) + c


# ---------------------------------------------------------------------------
# Per-transient kinetics
# ---------------------------------------------------------------------------

def get_time_at_level(time, signal, start_idx, end_idx, level, mode='rising'):
    if start_idx >= end_idx:
        return np.nan
    segment   = signal[start_idx:end_idx]
    t_segment = time[start_idx:end_idx]
    try:
        if mode == 'rising':
            matches = np.where(segment >= level)[0]
        else:
            matches = np.where(segment <= level)[0]

        if len(matches) == 0:
            return np.nan

        i = matches[0]
        if i == 0:
            return t_segment[0]

        y1, y2 = segment[i - 1], segment[i]
        t1, t2 = t_segment[i - 1], t_segment[i]
        if y2 == y1:
            return t1
        return t1 + (level - y1) / (y2 - y1) * (t2 - t1)
    except Exception:
        return np.nan


def extract_detailed_features(time_stamps, signal):
    sig_range = np.max(signal) - np.min(signal)
    if sig_range < 1e-6:
        return None

    max_peaks, props = find_peaks(signal, prominence=sig_range * 0.15)
    if len(max_peaks) == 0:
        return None
    peak_idx = max_peaks[np.argmax(props['prominences'])]

    min_peaks, _ = find_peaks(-signal)
    pre = min_peaks[min_peaks < peak_idx]

    if len(pre) > 0:
        start_idx = pre[-1]
    else:
        sig_min = np.min(signal[:peak_idx + 1])
        rough_amp = signal[peak_idx] - sig_min
        thresh5 = sig_min + 0.05 * rough_amp
        below = np.where(signal[:peak_idx] <= thresh5)[0]
        start_idx = int(below[-1]) if len(below) > 0 else 0

    baseline  = signal[start_idx]
    amp       = signal[peak_idx] - baseline
    t_start   = time_stamps[start_idx]
    peak_time = time_stamps[peak_idx]

    if len(max_peaks) >= 2:
        beat_period_frames = int(np.median(np.diff(max_peaks)))
    else:
        beat_period_frames = len(signal) // 2
    end_search = min(len(signal), peak_idx + int(beat_period_frames * 1.5))

    post = min_peaks[min_peaks > peak_idx]
    post_in_window = post[post < end_search]
    end_idx = int(post_in_window[0]) if len(post_in_window) > 0 else end_search

    levs = [baseline + x * amp for x in [0.1, 0.5, 0.9]]

    t_on  = [get_time_at_level(time_stamps, signal, start_idx, peak_idx + 1, l, 'rising')
             for l in levs]
    t_off = [get_time_at_level(time_stamps, signal, peak_idx, end_search, l, 'decay')
             for l in reversed(levs)]

    def dms(t1, t2):
        return abs(t1 - t2) * 1000 if not np.isnan(t1) and not np.isnan(t2) else np.nan

    cd = dms(t_off[2], t_on[0])
    cd_estimated = False
    if np.isnan(cd):
        w50 = dms(t_off[1], t_on[1])
        if not np.isnan(w50):
            cd = w50 * 1.6
            cd_estimated = True

    t_return = get_time_at_level(time_stamps, signal, peak_idx, end_search,
                                  baseline + 0.02 * amp, 'decay')
    T_OFF_ms = dms(t_return, peak_time)
    if np.isnan(T_OFF_ms) and end_idx < len(time_stamps):
        T_OFF_ms = dms(time_stamps[end_idx], peak_time)

    return {
        'BPM':      (len(max_peaks) / (time_stamps[-1] - time_stamps[0])) * 60
                    if (time_stamps[-1] - time_stamps[0]) > 0 else 0,
        'Amp':      amp,
        'F0':       baseline,
        'T_ON_ms':  (peak_time - t_start) * 1000,
        'T10_ON':   dms(t_on[0],  t_start),
        'T50_ON':   dms(t_on[1],  t_start),
        'T90_ON':   dms(t_on[2],  t_start),
        'T10_OFF':  dms(t_off[0], peak_time),
        'T50_OFF':  dms(t_off[1], peak_time),
        'T90_OFF':  dms(t_off[2], peak_time),
        'CD':       cd,
        'CD_estimated': cd_estimated,
        'T_OFF_ms': T_OFF_ms,
    }


def _fit_decay_tau(signal, time_stamps, peak_idx, end_idx, baseline, amp):
    d_sig  = signal[peak_idx: end_idx]
    d_time = time_stamps[peak_idx: end_idx] - time_stamps[peak_idx]
    if len(d_sig) < 5 or amp < 1e-9:
        return np.nan
    y_norm = (d_sig - baseline) / amp
    valid  = (y_norm > 0.05) & (y_norm < 0.95)
    if np.sum(valid) < 4:
        return np.nan
    try:
        slope, _ = np.polyfit(d_time[valid], np.log(np.clip(y_norm[valid], 1e-10, None)), 1)
        if slope >= 0:
            return np.nan
        return (-1.0 / slope) * 1000
    except Exception:
        return np.nan


def extract_beat_averaged_features(time_stamps, signal, beat_peaks):
    if beat_peaks is None or len(beat_peaks) == 0:
        return extract_detailed_features(time_stamps, signal)

    sig_range = np.max(signal) - np.min(signal)
    if sig_range < 1e-6:
        return None

    def dms(t1, t2):
        return abs(t1 - t2) * 1000 if not (np.isnan(t1) or np.isnan(t2)) else np.nan

    if len(beat_peaks) >= 2:
        med_period = int(np.median(np.diff(beat_peaks)))
    else:
        med_period = len(signal) // 2
    half_period = max(med_period // 2, 3)

    per_beat = []
    for peak_idx in beat_peaks:
        peak_idx = int(peak_idx)
        if peak_idx >= len(signal):
            continue

        lookback  = max(0, peak_idx - half_period)
        pre_seg   = signal[lookback: peak_idx]
        if len(pre_seg) == 0:
            continue
        start_rel = int(np.argmin(pre_seg))
        start_idx = lookback + start_rel

        baseline  = signal[start_idx]
        amp       = signal[peak_idx] - baseline
        if amp < sig_range * 0.05:
            continue

        t_start   = time_stamps[start_idx]
        peak_time = time_stamps[peak_idx]

        end_decay = min(len(signal), peak_idx + int(med_period * 1.5))
        next_peaks = beat_peaks[beat_peaks > peak_idx]
        if len(next_peaks) > 0:
            nxt = int(next_peaks[0])
            region = signal[peak_idx: min(nxt, end_decay)]
            if len(region) > 1:
                valley_rel = int(np.argmin(region))
                end_decay  = min(end_decay, peak_idx + valley_rel + 1)

        levs_rise = [baseline + f * amp for f in (0.1, 0.5, 0.9)]
        t10_on_t, t50_on_t, t90_on_t = [
            get_time_at_level(time_stamps, signal, start_idx, peak_idx + 1, lv, 'rising')
            for lv in levs_rise
        ]

        t10_off_t = get_time_at_level(time_stamps, signal, peak_idx, end_decay,
                                       baseline + 0.9 * amp, 'decay')
        t50_off_t = get_time_at_level(time_stamps, signal, peak_idx, end_decay,
                                       baseline + 0.5 * amp, 'decay')

        tau_ms = _fit_decay_tau(signal, time_stamps, peak_idx, end_decay, baseline, amp)

        if not np.isnan(tau_ms):
            T90_OFF  = tau_ms * np.log(10)
            T_OFF_ms = tau_ms * np.log(20)
        else:
            t90_off_t = get_time_at_level(time_stamps, signal, peak_idx, end_decay,
                                           baseline + 0.1 * amp, 'decay')
            T90_OFF   = dms(t90_off_t, peak_time)
            t_off_5   = get_time_at_level(time_stamps, signal, peak_idx, end_decay,
                                           baseline + 0.05 * amp, 'decay')
            T_OFF_ms  = dms(t_off_5, peak_time)

        per_beat.append({
            'Amp':      amp,
            'F0':       baseline,
            'T_ON_ms':  (peak_time - t_start) * 1000,
            'T10_ON':   dms(t10_on_t, t_start),
            'T50_ON':   dms(t50_on_t, t_start),
            'T90_ON':   dms(t90_on_t, t_start),
            'T10_OFF':  dms(t10_off_t, peak_time),
            'T50_OFF':  dms(t50_off_t, peak_time),
            'T90_OFF':  T90_OFF,
            'T_OFF_ms': T_OFF_ms,
        })

    if not per_beat:
        return extract_detailed_features(time_stamps, signal)

    keys = ['Amp', 'F0', 'T_ON_ms', 'T10_ON', 'T50_ON', 'T90_ON',
            'T10_OFF', 'T50_OFF', 'T90_OFF', 'T_OFF_ms']
    result = {}
    for k in keys:
        vals = [b[k] for b in per_beat if not np.isnan(b[k])]
        result[k] = float(np.mean(vals)) if vals else np.nan

    total_dur = time_stamps[-1] - time_stamps[0]
    result['BPM'] = (len(beat_peaks) / total_dur) * 60 if total_dur > 0 else 0.0

    t_on   = result.get('T_ON_ms',  np.nan)
    t_off  = result.get('T_OFF_ms', np.nan)
    t10_on = result.get('T10_ON',   np.nan)
    if not any(np.isnan(v) for v in (t_on, t_off, t10_on)):
        result['CD'] = t_on + t_off - t10_on
    else:
        result['CD'] = np.nan

    return result


def calculate_synchronicity(signals):
    if signals.shape[0] < 2:
        return 0.0
    corr     = np.corrcoef(signals)
    n        = corr.shape[0]
    off_diag = corr[np.triu_indices(n, k=1)]
    return float(np.clip(np.nanmean(off_diag), 0.0, 1.0)) if len(off_diag) > 0 else 0.0


# ---------------------------------------------------------------------------
# Spatiotemporal analysis pipeline
# ---------------------------------------------------------------------------

def compute_pulsatility_map(corrected_signals, H_bin, W_bin, T_bin, fps_eff):
    f_low  = 0.3
    f_high = 5.0
    nyq    = fps_eff / 2.0
    fh     = min(f_high, nyq * 0.9)

    filtered_traces = corrected_signals.copy()
    filter_applied  = False

    if f_low < fh and nyq > f_low:
        try:
            sos = butter(4, [f_low / nyq, fh / nyq], btype='band', output='sos')
            tmp = np.zeros_like(corrected_signals)
            n_ok = 0
            for i in range(len(corrected_signals)):
                try:
                    tmp[i] = sosfiltfilt(sos, corrected_signals[i])
                    n_ok += 1
                except Exception:
                    tmp[i] = corrected_signals[i]
            filtered_traces = tmp
            filter_applied  = n_ok > 0
        except Exception:
            pass

    pulsatility_map = np.var(filtered_traces, axis=1).reshape(H_bin, W_bin)
    return pulsatility_map, filtered_traces


def compute_activity_mask(pulsatility_map):
    p_flat = pulsatility_map.ravel()

    if np.ptp(p_flat) < 1e-10:
        thresh = np.percentile(p_flat, 65)
        return pulsatility_map > thresh

    try:
        thresh = threshold_otsu(pulsatility_map)
        mask   = pulsatility_map > thresh
        frac   = np.mean(mask)
        if frac < 0.03 or frac > 0.85:
            thresh = np.percentile(p_flat, 65)
            mask   = pulsatility_map > thresh
    except Exception:
        thresh = np.percentile(p_flat, 65)
        mask   = pulsatility_map > thresh

    return mask


def compute_reference_trace(corrected_signals, activity_mask):
    mask_flat = activity_mask.flatten()
    active    = corrected_signals[mask_flat]
    if len(active) == 0:
        return np.mean(corrected_signals, axis=0)
    return np.mean(active, axis=0)


def detect_beats(reference_trace, time_stamps, fps_eff):
    sig_range = np.max(reference_trace) - np.min(reference_trace)
    if sig_range < 1e-6:
        return np.array([], dtype=int)

    min_dist = max(int(fps_eff * 0.8), 2)
    peaks, _ = find_peaks(
        reference_trace,
        prominence=sig_range * 0.15,
        distance=min_dist,
    )
    return peaks


def compute_activation_time_map(corrected_signals, activity_mask, beat_peaks,
                                 time_stamps, fps_eff):
    H_bin, W_bin = activity_mask.shape
    N            = H_bin * W_bin
    mask_flat    = activity_mask.flatten()
    active_idx   = np.where(mask_flat)[0]

    if len(beat_peaks) == 0 or len(active_idx) == 0:
        return np.full((H_bin, W_bin), np.nan)

    lookback = int(fps_eff * 3.0)

    beat_maps = []
    for peak_idx in beat_peaks:
        start     = max(0, peak_idx - lookback)
        act_times = np.full(N, np.nan)

        for i in active_idx:
            seg = corrected_signals[i, start: peak_idx + 1]
            if len(seg) < 2:
                continue

            peak_val  = seg[-1]
            baseline  = np.min(seg)
            amp       = peak_val - baseline
            if amp < 1e-6:
                continue

            threshold = baseline + 0.5 * amp
            matches   = np.where(seg >= threshold)[0]
            if len(matches) == 0:
                continue

            i_cross = matches[0]
            if i_cross == 0:
                act_times[i] = time_stamps[start]
            else:
                y1, y2 = seg[i_cross - 1], seg[i_cross]
                t1     = time_stamps[start + i_cross - 1]
                t2     = time_stamps[start + i_cross]
                if y2 > y1:
                    frac          = (threshold - y1) / (y2 - y1)
                    act_times[i]  = t1 + frac * (t2 - t1)
                else:
                    act_times[i] = t1

        valid = act_times[~np.isnan(act_times)]
        if len(valid) > 0:
            act_times -= np.nanmin(act_times)

        beat_maps.append(act_times.reshape(H_bin, W_bin))

    mean_map    = np.nanmean(np.array(beat_maps), axis=0)
    mean_map_ms = mean_map * 1000.0
    return mean_map_ms


def extract_spatiotemporal_features(corrected_signals, filtered_traces,
                                     activity_mask, beat_peaks, time_stamps, fps_eff):
    mask_flat  = activity_mask.flatten()
    active_idx = np.where(mask_flat)[0]

    if len(active_idx) == 0:
        return np.array([]), active_idx

    T     = corrected_signals.shape[1]
    dt    = (time_stamps[-1] - time_stamps[0]) / max(T - 1, 1)
    freqs = np.fft.rfftfreq(T, d=dt)
    band  = (freqs >= 0.3) & (freqs <= 5.0)
    win   = max(int(fps_eff * 1.5), 3)

    features = []
    for i in active_idx:
        sig  = corrected_signals[i]
        filt = filtered_traces[i]

        puls = float(np.var(filt))

        fft_mag = np.abs(np.fft.rfft(filt))
        dom_freq = float(freqs[band][np.argmax(fft_mag[band])]) \
                   if np.any(band) and np.any(fft_mag[band] > 0) else 0.0

        if len(beat_peaks) > 0:
            amps = [float(sig[p] - np.min(sig[max(0, p - win): p + 1])) for p in beat_peaks]
            mean_amp = float(np.mean(amps))
        else:
            mean_amp = float(np.max(sig) - np.min(sig))

        try:
            phase_offset = float(np.mean(np.angle(hilbert(filt))))
        except Exception:
            phase_offset = 0.0

        sr = np.max(sig) - np.min(sig)
        duty = float(np.mean(sig > (np.min(sig) + 0.5 * sr))) if sr > 1e-6 else 0.0

        features.append([puls, dom_freq, mean_amp, phase_offset, duty])

    return np.array(features, dtype=np.float64), active_idx


def cluster_spatiotemporal(features, active_idx, total_bins):
    """Original HDBSCAN clustering on hand-crafted features (baseline method)."""
    labels_full = np.full(total_bins, -2, dtype=int)

    if len(features) < 3:
        return labels_full

    col_std   = np.std(features, axis=0)
    good_cols = col_std > 1e-10
    if not np.any(good_cols):
        labels_full[active_idx] = 0
        return labels_full

    feat_valid  = features[:, good_cols]
    feat_scaled = StandardScaler().fit_transform(feat_valid)
    feat_scaled = np.nan_to_num(feat_scaled, nan=0.0, posinf=0.0, neginf=0.0)

    try:
        import hdbscan
        min_size  = max(3, len(feat_scaled) // 15)
        clusterer = hdbscan.HDBSCAN(min_cluster_size=min_size, min_samples=2)
        labels    = clusterer.fit_predict(feat_scaled)
    except ImportError:
        from sklearn.cluster import KMeans
        n_c    = min(4, max(2, len(feat_scaled) // 5))
        labels = KMeans(n_clusters=n_c, n_init=10, random_state=42).fit_predict(feat_scaled)

    labels_full[active_idx] = labels
    return labels_full


# ---------------------------------------------------------------------------
# NCC Graph clustering (Option 2)
# ---------------------------------------------------------------------------

def compute_ncc_adjacency(corrected_signals, activity_mask, max_lag_fraction=0.25):
    """
    Build a sparse 4-connected pixel adjacency graph weighted by peak normalized
    cross-correlation (NCC).

    Signals are L2-normalised (zero-mean, unit-norm) before correlation so that:
      - Identical signals  → NCC = 1.0
      - Orthogonal signals → NCC = 0.0
      - Anti-correlated    → NCC < 0, clipped to 0 (not connected)

    Returns
    -------
    affinity  : scipy.sparse.csr_matrix (N_active × N_active), values ∈ [0, 1]
    active_idx : (N_active,) int
    """
    H_bin, W_bin = activity_mask.shape
    mask_flat    = activity_mask.flatten()
    active_idx   = np.where(mask_flat)[0]
    N_active     = len(active_idx)

    if N_active < 2:
        return csr_matrix((max(N_active, 0), max(N_active, 0))), active_idx

    global_to_local = np.full(H_bin * W_bin, -1, dtype=np.int32)
    global_to_local[active_idx] = np.arange(N_active, dtype=np.int32)

    T       = corrected_signals.shape[1]
    max_lag = max(1, int(T * max_lag_fraction))

    sigs = corrected_signals[active_idx].copy().astype(np.float64)
    sigs -= sigs.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(sigs, axis=1, keepdims=True)
    norms[norms < 1e-10] = 1.0
    sigs /= norms

    affinity = lil_matrix((N_active, N_active), dtype=np.float32)
    center   = T - 1

    for local_i in range(N_active):
        global_i = int(active_idx[local_i])
        row, col = divmod(global_i, W_bin)

        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = row + dr, col + dc
            if not (0 <= nr < H_bin and 0 <= nc < W_bin):
                continue
            global_j = nr * W_bin + nc
            local_j  = int(global_to_local[global_j])
            if local_j < 0 or local_j <= local_i:
                continue

            cc      = correlate(sigs[local_i], sigs[local_j], mode='full', method='fft')
            window  = cc[center - max_lag: center + max_lag + 1]
            ncc_val = float(np.max(window))
            ncc_val = max(0.0, min(1.0, ncc_val))

            affinity[local_i, local_j] = ncc_val
            affinity[local_j, local_i] = ncc_val

    return csr_matrix(affinity), active_idx


def cluster_ncc_graph(ncc_matrix, active_idx, total_bins, resolution=1.0):
    """
    Apply Louvain community detection on the NCC (or embedding) similarity graph.

    Fallback cascade: Louvain → Spectral → HDBSCAN → single cluster.

    Label conventions: -2=inactive, 0,1,2...=community IDs.
    """
    labels_full = np.full(total_bins, -2, dtype=int)
    N = len(active_idx)

    if N == 0:
        return labels_full
    if N == 1:
        labels_full[active_idx[0]] = 0
        return labels_full

    # ── Attempt 1: Louvain ────────────────────────────────────────────────────
    try:
        import networkx as nx
        import community as community_louvain

        G         = nx.from_scipy_sparse_array(ncc_matrix, edge_attribute='weight')
        partition = community_louvain.best_partition(
            G, weight='weight', resolution=resolution, random_state=42
        )
        labels = np.array([partition.get(i, 0) for i in range(N)], dtype=int)
        labels_full[active_idx] = labels
        return labels_full
    except Exception:
        pass

    # ── Attempt 2: Spectral clustering ───────────────────────────────────────
    try:
        from sklearn.cluster import SpectralClustering
        dense = np.clip(ncc_matrix.toarray(), 0.0, 1.0).astype(np.float64)
        n_c = min(8, max(2, N // 10))
        sc  = SpectralClustering(
            n_clusters=n_c, affinity='precomputed',
            assign_labels='kmeans', random_state=42, n_init=10
        )
        labels = sc.fit_predict(dense)
        labels_full[active_idx] = labels
        return labels_full
    except Exception:
        pass

    # ── Attempt 3: HDBSCAN ───────────────────────────────────────────────────
    try:
        import hdbscan
        dense = np.clip(ncc_matrix.toarray(), 0.0, 1.0).astype(np.float64)
        dist  = 1.0 - dense
        np.fill_diagonal(dist, 0.0)
        min_s  = max(3, N // 15)
        labels = hdbscan.HDBSCAN(
            metric='precomputed', min_cluster_size=min_s, min_samples=2
        ).fit_predict(dist)
        labels[labels == -1] = labels.max() + 1
        labels_full[active_idx] = labels
        return labels_full
    except Exception:
        pass

    labels_full[active_idx] = 0
    return labels_full


def compute_cluster_ncc_score(ncc_matrix, labels_local):
    """
    Mean intra-cluster edge weight — quality metric for graph-based clustering.
    Works for both NCC Graph (NCC weights) and SimCLR Graph (cosine similarity weights).

    Returns score ∈ [0, 1].
    """
    if ncc_matrix.nnz == 0:
        return 0.0

    cx   = ncc_matrix.tocoo()
    rows = cx.row
    cols = cx.col
    vals = cx.data

    mask = labels_local[rows] == labels_local[cols]
    if not np.any(mask):
        return 0.0

    return float(np.mean(vals[mask]))


# ---------------------------------------------------------------------------
# SimCLR Contrastive Encoder (Option 3)
# ---------------------------------------------------------------------------

if TORCH_AVAILABLE:
    class SimCLREncoder(nn.Module):
        """
        1D CNN encoder for calcium signal contrastive representation learning.

        Architecture:
          Conv1d(1→32, k=7, pad=3) → BN → ReLU → MaxPool1d(2)
          Conv1d(32→64, k=5, pad=2) → BN → ReLU → MaxPool1d(2)
          Conv1d(64→128, k=3, pad=1) → BN → ReLU → AdaptiveAvgPool1d(1)
          → h ∈ ℝ¹²⁸  (representation used at inference)
          Projection head (training only):
            Linear(128→128) → ReLU → Linear(128→64) → L2-normalise
        """
        def __init__(self):
            super().__init__()
            self.encoder = nn.Sequential(
                nn.Conv1d(1, 32, kernel_size=7, padding=3),
                nn.BatchNorm1d(32),
                nn.ReLU(inplace=True),
                nn.MaxPool1d(2),
                nn.Conv1d(32, 64, kernel_size=5, padding=2),
                nn.BatchNorm1d(64),
                nn.ReLU(inplace=True),
                nn.MaxPool1d(2),
                nn.Conv1d(64, 128, kernel_size=3, padding=1),
                nn.BatchNorm1d(128),
                nn.ReLU(inplace=True),
                nn.AdaptiveAvgPool1d(1),
            )
            self.proj_head = nn.Sequential(
                nn.Linear(128, 128),
                nn.ReLU(inplace=True),
                nn.Linear(128, 64),
            )

        def forward(self, x, project=False):
            # x: (B, T) → unsqueeze channel → (B, 1, T)
            if x.ndim == 2:
                x = x.unsqueeze(1)
            h = self.encoder(x).squeeze(-1)   # (B, 128)
            if project:
                z = self.proj_head(h)
                z = F.normalize(z, dim=1)
                return z
            return h
else:
    SimCLREncoder = None


def calcium_augment(signals_batch, T):
    """
    Apply one random augmentation per sample in a batch of calcium signals.

    Augmentation pool:
      0. Circular phase shift    — ±50 % of T (simulates recording start offset)
      1. Amplitude scale         — ×U(0.6, 1.4) (intensity calibration variation)
      2. Gaussian noise          — σ = 5 % of signal range (detector noise)
      3. Slow sinusoidal drift   — ±10 % amplitude, random phase (photobleach residual)

    Parameters
    ----------
    signals_batch : (B, T) float32 numpy array — z-scored active pixel signals
    T             : int — signal length

    Returns
    -------
    augmented : (B, T) float32 numpy array
    """
    B   = signals_batch.shape[0]
    out = signals_batch.copy()

    for i in range(B):
        sig      = out[i].copy()
        aug_type = np.random.randint(0, 4)

        if aug_type == 0:
            shift = np.random.randint(-T // 2, T // 2 + 1)
            sig   = np.roll(sig, shift)
        elif aug_type == 1:
            sig = sig * np.random.uniform(0.6, 1.4)
        elif aug_type == 2:
            sig_range = max(float(np.ptp(sig)), 1e-6)
            sig = sig + np.random.randn(T).astype(np.float32) * 0.05 * sig_range
        else:
            sig_range = max(float(np.ptp(sig)), 1e-6)
            t_arr     = np.linspace(0.0, 2.0 * np.pi, T, dtype=np.float32)
            phase     = np.random.uniform(0.0, 2.0 * np.pi)
            drift     = (0.1 * sig_range * np.sin(t_arr + phase)).astype(np.float32)
            sig       = sig + drift

        out[i] = sig

    return out


def _nt_xent_loss(z_i, z_j, temperature=0.5):
    """
    NT-Xent contrastive loss over a batch of paired embedding vectors.

    For a batch of B pairs: 2B embeddings total; the positive pair for sample i
    is i+B (and vice versa). All other 2B-2 entries are negatives.

    Parameters
    ----------
    z_i, z_j    : (B, D) L2-normalised torch tensors — two augmented views
    temperature : float — sharpens the softmax; lower → harder negatives

    Returns
    -------
    loss : scalar torch tensor
    """
    B   = z_i.shape[0]
    z   = torch.cat([z_i, z_j], dim=0)                         # (2B, D)
    sim = torch.mm(z, z.T) / temperature                        # (2B, 2B)

    # Mask diagonal (self-similarity → -inf so it never wins)
    mask = torch.eye(2 * B, device=z.device, dtype=torch.bool)
    sim.masked_fill_(mask, float('-inf'))

    # Positive pair index: for row i, the positive is at i+B (or i-B for second half)
    labels = torch.arange(B, device=z.device)
    labels = torch.cat([labels + B, labels])                    # (2B,)

    return F.cross_entropy(sim, labels)


def train_simclr(corrected_signals, activity_mask, n_epochs=100, progress_cb=None):
    """
    Transductive SimCLR training on the active pixel signals for this recording.

    The encoder is trained from scratch on augmented views of the active pixel
    calcium traces.  No pre-training or labels are required.  The resulting
    h ∈ ℝ¹²⁸ representations capture waveform shape, phase, and amplitude
    structure independently of firing rate.

    Parameters
    ----------
    corrected_signals : (N_total, T) float64 — photobleach-corrected bin traces
    activity_mask     : (H_bin, W_bin) bool
    n_epochs          : int — contrastive training iterations (default 100)
    progress_cb       : callable(epoch: int) or None — called after each epoch

    Returns
    -------
    encoder : SimCLREncoder in eval mode, on CPU
    """
    if not TORCH_AVAILABLE:
        raise ImportError(
            "PyTorch is required for SimCLR Graph clustering.\n"
            "Install with:  pip install torch  (CPU-only)\n"
            "or:  pip install torch --index-url https://download.pytorch.org/whl/cu118"
        )

    mask_flat  = activity_mask.flatten()
    active_idx = np.where(mask_flat)[0]

    encoder = SimCLREncoder()

    if len(active_idx) < 2:
        encoder.eval()
        return encoder

    # Prepare signals: (N_active, T) float32, z-scored per signal
    sigs  = corrected_signals[active_idx].copy().astype(np.float32)
    means = sigs.mean(axis=1, keepdims=True)
    stds  = sigs.std(axis=1, keepdims=True)
    stds[stds < 1e-10] = 1.0
    sigs  = (sigs - means) / stds

    T      = sigs.shape[1]
    N      = len(sigs)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    encoder = encoder.to(device)

    optimizer = torch.optim.Adam(encoder.parameters(), lr=3e-4, weight_decay=1e-5)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=n_epochs, eta_min=1e-5
    )

    # Batch size: use all active pixels when they fit in memory (typically < 1000)
    batch_size = min(N, 256)

    encoder.train()
    for epoch in range(n_epochs):
        if N > batch_size:
            idx = np.random.choice(N, size=batch_size, replace=False)
            batch = sigs[idx]
        else:
            batch = sigs

        view_i = calcium_augment(batch, T)
        view_j = calcium_augment(batch, T)

        x_i = torch.from_numpy(view_i).to(device)
        x_j = torch.from_numpy(view_j).to(device)

        z_i = encoder(x_i, project=True)
        z_j = encoder(x_j, project=True)

        loss = _nt_xent_loss(z_i, z_j, temperature=0.5)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        scheduler.step()

        if progress_cb is not None:
            progress_cb(epoch + 1)

    encoder.eval()
    # Move back to CPU so downstream numpy ops don't need GPU awareness
    encoder = encoder.cpu()
    return encoder


def compute_embedding_adjacency(encoder, corrected_signals, activity_mask):
    """
    Build a sparse 4-connected pixel adjacency graph weighted by cosine similarity
    of SimCLR CNN embeddings h_i, h_j.

    Drop-in replacement for compute_ncc_adjacency() — same return signature so
    cluster_ncc_graph() and compute_cluster_ncc_score() work unchanged.

    Parameters
    ----------
    encoder           : trained SimCLREncoder (eval mode, on CPU)
    corrected_signals : (N_total, T) float64
    activity_mask     : (H_bin, W_bin) bool

    Returns
    -------
    affinity  : scipy.sparse.csr_matrix (N_active × N_active), values ∈ [0, 1]
    active_idx : (N_active,) int
    """
    if not TORCH_AVAILABLE:
        raise ImportError("PyTorch required for SimCLR Graph.")

    H_bin, W_bin = activity_mask.shape
    mask_flat    = activity_mask.flatten()
    active_idx   = np.where(mask_flat)[0]
    N_active     = len(active_idx)

    if N_active < 2:
        return csr_matrix((max(N_active, 0), max(N_active, 0))), active_idx

    # Get embeddings h ∈ ℝ¹²⁸ for all active pixels
    sigs  = corrected_signals[active_idx].copy().astype(np.float32)
    means = sigs.mean(axis=1, keepdims=True)
    stds  = sigs.std(axis=1, keepdims=True)
    stds[stds < 1e-10] = 1.0
    sigs  = (sigs - means) / stds

    with torch.no_grad():
        x          = torch.from_numpy(sigs)
        h          = encoder(x, project=False)      # (N_active, 128)
        h          = F.normalize(h, dim=1)          # unit vectors → dot = cosine sim
        embeddings = h.numpy()                      # (N_active, 128)

    # Build 4-connected graph: same spatial structure as NCC Graph
    global_to_local = np.full(H_bin * W_bin, -1, dtype=np.int32)
    global_to_local[active_idx] = np.arange(N_active, dtype=np.int32)

    affinity = lil_matrix((N_active, N_active), dtype=np.float32)

    for local_i in range(N_active):
        global_i = int(active_idx[local_i])
        row, col = divmod(global_i, W_bin)

        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = row + dr, col + dc
            if not (0 <= nr < H_bin and 0 <= nc < W_bin):
                continue
            global_j = nr * W_bin + nc
            local_j  = int(global_to_local[global_j])
            if local_j < 0 or local_j <= local_i:
                continue

            cos_sim = float(np.dot(embeddings[local_i], embeddings[local_j]))
            cos_sim = max(0.0, min(1.0, cos_sim))

            affinity[local_i, local_j] = cos_sim
            affinity[local_j, local_i] = cos_sim

    return csr_matrix(affinity), active_idx


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class AnalysisWorker(QThread):
    finished = Signal(dict)
    error    = Signal(str)
    progress = Signal(int)

    def __init__(self, file_path, params):
        super().__init__()
        self.path   = file_path
        self.params = params

    def run(self):
        try:
            self.progress.emit(10)
            raw_stack = load_image(self.path)

            if raw_stack.ndim == 2:
                raw_stack = raw_stack[np.newaxis, ...]

            T_raw, H_raw, W_raw = raw_stack.shape

            is_fps = self.params.get('use_fps', False)
            if is_fps:
                fps            = self.params.get('val', 10.0)
                total_duration = T_raw / fps
            else:
                total_duration = self.params.get('val', 30.0)
                fps            = T_raw / total_duration

            sample = raw_stack[:min(16, T_raw)]
            hmin, hmax = np.percentile(sample, [0.4, 99.6])
            if hmax <= hmin:
                hmax = hmin + 1.0
            raw_stack  = np.clip(raw_stack, hmin, hmax)
            raw_stack  = (raw_stack - hmin) / (hmax - hmin) * 255.0

            self.progress.emit(20)

            b_size = self.params['binSize']
            sigma  = H_raw / 204.8
            frames = [block_reduce(gaussian_filter(f, sigma), (b_size, b_size), np.mean)
                      for f in raw_stack]
            binned_stack = np.array(frames)

            T_bin, H_bin, W_bin = binned_stack.shape
            fps_bin     = T_bin / total_duration
            time_stamps = np.linspace(0, total_duration, T_bin)

            self.progress.emit(30)

            raw_signals       = binned_stack.reshape(T_bin, -1).T
            corrected_signals = np.zeros_like(raw_signals)

            for i, sig in enumerate(raw_signals):
                try:
                    if self.params['model'] == 'Single Exp':
                        env = _bleach_envelope(sig, T_bin)
                        try:
                            a0   = max(float(env[0] - env[-1]), 1e-3)
                            p0   = [a0, total_duration / 3.0, float(env[-1])]
                            popt, _ = curve_fit(
                                _single_exp, time_stamps, env, p0=p0, maxfev=2000,
                                bounds=([0, 1e-5, -np.inf], [np.inf, np.inf, np.inf]),
                            )
                            baseline = _single_exp(time_stamps, *popt)
                        except Exception:
                            baseline = env
                    else:
                        min_idx, _ = find_peaks(-sig, distance=max(5, len(sig) // 10))
                        if len(min_idx) > 1:
                            f_i = interp1d(time_stamps[min_idx], sig[min_idx],
                                           kind='linear', fill_value="extrapolate")
                            baseline = f_i(time_stamps)
                        else:
                            baseline = _bleach_envelope(sig, T_bin)
                    corrected_signals[i] = sig - baseline
                except Exception:
                    corrected_signals[i] = sig - np.min(sig)

            self.progress.emit(50)

            nan_frac = np.mean(~np.isfinite(corrected_signals))
            if nan_frac > 0.9:
                raise ValueError(
                    "Corrected signals are mostly NaN/Inf. "
                    "Check that the file has valid pixel values and FPS is set correctly."
                )
            corrected_signals = np.nan_to_num(corrected_signals, nan=0.0, posinf=0.0, neginf=0.0)

            pulsatility_map, filtered_traces = compute_pulsatility_map(
                corrected_signals, H_bin, W_bin, T_bin, fps_bin
            )
            activity_mask = compute_activity_mask(pulsatility_map)

            reference_trace = compute_reference_trace(corrected_signals, activity_mask)
            beat_peaks      = detect_beats(reference_trace, time_stamps, fps_bin)

            self.progress.emit(65)

            activation_map = compute_activation_time_map(
                corrected_signals, activity_mask, beat_peaks, time_stamps, fps_bin
            )

            features, active_idx = extract_spatiotemporal_features(
                corrected_signals, filtered_traces, activity_mask,
                beat_peaks, time_stamps, fps_bin
            )

            self.progress.emit(75)

            # ── Clustering: always run all three methods ─────────────────────
            n_total   = H_bin * W_bin
            n_epochs  = self.params.get('n_simclr_epochs', 100)
            ncc_score = None
            emb_score = None

            def _make_clu_map(labels_full):
                m = np.zeros(n_total, dtype=int)
                for idx in active_idx:
                    lbl = labels_full[idx]
                    m[idx] = 1 if lbl == -1 else lbl + 2
                return m.reshape(H_bin, W_bin)

            # --- HDBSCAN ---
            labels_hdbscan = cluster_spatiotemporal(features, active_idx, n_total)
            clu_map_hdbscan = _make_clu_map(labels_hdbscan)
            self.progress.emit(79)

            # --- NCC Graph ---
            labels_ncc = labels_hdbscan.copy()
            clu_map_ncc = clu_map_hdbscan.copy()
            if len(active_idx) >= 2:
                try:
                    ncc_matrix, _ = compute_ncc_adjacency(corrected_signals, activity_mask)
                    labels_ncc    = cluster_ncc_graph(ncc_matrix, active_idx, n_total)
                    clu_map_ncc   = _make_clu_map(labels_ncc)
                    if len(active_idx) > 0:
                        ncc_score = compute_cluster_ncc_score(ncc_matrix, labels_ncc[active_idx])
                except Exception:
                    pass
            self.progress.emit(88)

            # --- SimCLR Graph ---
            labels_simclr = labels_ncc.copy()
            clu_map_simclr = clu_map_ncc.copy()
            if TORCH_AVAILABLE and len(active_idx) >= 2:
                try:
                    def _simclr_progress(epoch):
                        self.progress.emit(88 + int(epoch / max(n_epochs, 1) * 11))

                    encoder = train_simclr(
                        corrected_signals, activity_mask, n_epochs, _simclr_progress
                    )
                    emb_matrix, _ = compute_embedding_adjacency(
                        encoder, corrected_signals, activity_mask
                    )
                    labels_simclr  = cluster_ncc_graph(emb_matrix, active_idx, n_total)
                    clu_map_simclr = _make_clu_map(labels_simclr)
                    if len(active_idx) > 0:
                        emb_score = compute_cluster_ncc_score(emb_matrix, labels_simclr[active_idx])
                except Exception:
                    pass

            self.progress.emit(99)

            # primary labels for activity-mask-based operations (e.g. random_sample)
            labels_full = labels_hdbscan
            clu_map     = clu_map_hdbscan   # kept for backward compat

            active_filtered = filtered_traces[active_idx] if len(active_idx) > 0 \
                              else filtered_traces[:0]
            sync_index = calculate_synchronicity(active_filtered) if len(active_idx) > 1 else 0.0

            self.progress.emit(100)

            self.finished.emit({
                'clu_map':           clu_map,            # backward compat (HDBSCAN)
                'clu_map_hdbscan':   clu_map_hdbscan,
                'clu_map_ncc':       clu_map_ncc,
                'clu_map_simclr':    clu_map_simclr,
                'labels':            labels_full,
                'labels_hdbscan':    labels_hdbscan,
                'labels_ncc':        labels_ncc,
                'labels_simclr':     labels_simclr,
                'corrected_signals': corrected_signals,
                'filtered_traces':   filtered_traces,
                'time':              time_stamps,
                'dims':              (H_bin, W_bin),
                'activity_mask':     activity_mask,
                'pulsatility_map':   pulsatility_map,
                'activation_map':    activation_map,
                'beat_peaks':        beat_peaks,
                'reference_trace':   reference_trace,
                'sync_index':        sync_index,
                'beat_count':        len(beat_peaks),
                'cluster_method':    'All',
                'ncc_score':         ncc_score,
                'emb_score':         emb_score,
            })

        except Exception as e:
            print("\n CRASH IN BACKGROUND THREAD:")
            traceback.print_exc()
            self.error.emit(str(e))


class BatchWorker(QThread):
    finished      = Signal(object)
    error         = Signal(str)
    progress      = Signal(int)
    file_progress = Signal(str)

    def __init__(self, file_paths, params):
        super().__init__()
        self.file_paths = file_paths
        self.params     = params

    def run(self):
        all_rows   = []
        total_files = len(self.file_paths)
        samples_per_file = self.params.get('batch_samples', 10)

        for f_idx, path in enumerate(self.file_paths):
            try:
                fname = os.path.basename(path)
                self.file_progress.emit(f"Processing {f_idx + 1}/{total_files}: {fname}")

                raw_stack = load_image(path)
                if raw_stack.ndim == 2:
                    raw_stack = raw_stack[np.newaxis, ...]

                fps    = self.params.get('val', 10.0)
                is_fps = self.params.get('use_fps', True)
                if path.lower().endswith(('.tif', '.tiff')):
                    try:
                        with tifffile.TiffFile(path) as tif:
                            ij_meta = tif.imagej_metadata or {}
                            if 'finterval' in ij_meta and ij_meta['finterval'] > 0:
                                fps    = 1.0 / ij_meta['finterval']
                                is_fps = True
                    except Exception:
                        pass

                T_raw = raw_stack.shape[0]
                total_duration = T_raw / fps if is_fps else self.params.get('val', 30.0)

                T, H, W = raw_stack.shape
                b_size  = 32 if max(H, W) >= 1024 else 16

                sample = raw_stack[:min(16, T)]
                hmin, hmax = np.percentile(sample, [0.4, 99.6])
                raw_stack  = np.clip(raw_stack, hmin, hmax)
                raw_stack  = (raw_stack - hmin) / (hmax - hmin) * 255.0

                sigma  = H / 204.8
                frames = [block_reduce(gaussian_filter(f, sigma), (b_size, b_size), np.mean)
                          for f in raw_stack]
                binned_stack = np.array(frames)

                T_bin, H_bin, W_bin = binned_stack.shape
                fps_bin     = T_bin / total_duration
                time_stamps = np.linspace(0, total_duration, T_bin)

                raw_signals       = binned_stack.reshape(T_bin, -1).T
                corrected_signals = np.zeros_like(raw_signals)

                for i, sig in enumerate(raw_signals):
                    try:
                        min_idx, _ = find_peaks(-sig, distance=max(5, len(sig) // 10))
                        if len(min_idx) > 1:
                            f_i = interp1d(time_stamps[min_idx], sig[min_idx],
                                           kind='linear', fill_value="extrapolate")
                            baseline = f_i(time_stamps)
                        else:
                            baseline = _bleach_envelope(sig, T_bin)
                        corrected_signals[i] = sig - baseline
                    except Exception:
                        corrected_signals[i] = sig - np.min(sig)

                pulsatility_map, _ = compute_pulsatility_map(
                    corrected_signals, H_bin, W_bin, T_bin, fps_bin
                )
                activity_mask = compute_activity_mask(pulsatility_map)
                active_idx    = np.where(activity_mask.flatten())[0]

                if len(active_idx) == 0:
                    continue

                valid_sigs = corrected_signals[active_idx]
                amps       = np.max(valid_sigs, axis=1) - np.min(valid_sigs, axis=1)
                weights    = amps ** 2
                probs      = weights / np.sum(weights) if np.sum(weights) > 0 else None

                n_choose      = min(samples_per_file, len(active_idx))
                chosen_indices = np.random.choice(active_idx, size=n_choose, replace=False, p=probs)

                for idx in chosen_indices:
                    m = extract_detailed_features(time_stamps, corrected_signals[idx])
                    if m:
                        y, x = divmod(int(idx), W_bin)
                        m.update({
                            'Filename':    fname,
                            'X (Binned)': x,
                            'Y (Binned)': y,
                            'ID':          idx,
                        })
                        all_rows.append(m)

                self.progress.emit(int(((f_idx + 1) / total_files) * 100))

            except Exception as e:
                print(f"Error processing {path}: {e}")
                continue

        df = pd.DataFrame(all_rows)
        if not df.empty:
            priority = ['Filename', 'ID', 'X (Binned)', 'Y (Binned)']
            rest     = [c for c in df.columns if c not in priority]
            df       = df[priority + rest]

        self.finished.emit(df)
