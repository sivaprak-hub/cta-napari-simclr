# CTA SimCLR — Calcium Transient Analyzer with Contrastive Learning

A napari plugin for clustering calcium-transient imaging data using three complementary methods run simultaneously:

| Method | Approach | Quality score |
|---|---|---|
| **HDBSCAN** | Feature-based (pulsatility, frequency, amplitude, phase, duty cycle) | — |
| **NCC Graph** | Pixel-to-pixel cross-correlation + Louvain community detection | Intra-cluster NCC |
| **SimCLR Graph** | Self-supervised 1-D CNN embeddings + Louvain community detection | Intra-cluster cosine similarity |

All three cluster maps are shown as separate named layers in napari after each analysis run.

---

## Requirements

- Python **3.10 – 3.12**
- Windows / macOS / Linux
- A Qt back-end: PyQt5, PyQt6, PySide2, or PySide6

---

## Installation

### 1. Create and activate a virtual environment

```bash
python -m venv cta_env

# Windows
cta_env\Scripts\activate

# macOS / Linux
source cta_env/bin/activate
```

### 2. Install dependencies

**CPU-only (recommended for most users):**

```bash
pip install -r requirements.txt
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

**CUDA 11.8 (NVIDIA GPU acceleration for SimCLR training):**

```bash
pip install -r requirements.txt
pip install torch --index-url https://download.pytorch.org/whl/cu118
```

> **Note:** If `torch` is already listed in `requirements.txt`, pip will use the default PyPI wheel (CPU). Override it with the `--index-url` flag above after the initial install.

### 3. Verify the install

```bash
python - <<'EOF'
import napari, torch, hdbscan, community
print("napari", napari.__version__)
print("torch", torch.__version__, "| CUDA:", torch.cuda.is_available())
print("hdbscan", hdbscan.__version__)
print("All OK")
EOF
```

---

## Running the app

```bash
# From the repo root, with the virtual environment active:
python run_cta_simclr.py
```

The napari viewer opens with the **CTA Controls** panel docked on the right.

---

## Usage

1. **Load a recording** — click *Open File* and select a `.tif` / `.tiff` stack (T × H × W).
2. **Set parameters** — adjust spatial binning, frame rate / interval, and SimCLR epoch count.
3. **Run analysis** — click *Run Analysis*. A progress bar tracks preprocessing → HDBSCAN → NCC Graph → SimCLR training → done.
4. **Inspect results** — three label layers appear in the napari layer list:
   - `Clusters (HDBSCAN)`
   - `Clusters (NCC)`
   - `Clusters (SimCLR)`
5. **Quality scores** — NCC cluster score and SimCLR embedding score are shown in the panel.
6. **Batch mode** — use the *Batch* tab to process a folder of recordings and export CSV summaries.

---

## Project structure

```
cta_napari_simclr/
├── run_cta_simclr.py          # Standalone launcher
├── requirements.txt
├── README.md
└── CTA_SimCLR/
    ├── __init__.py
    ├── backend.py             # AnalysisWorker, clustering algorithms, SimCLR model
    └── widget.py              # napari Qt UI
```

---

## Key hyperparameters

| Parameter | Default | Location |
|---|---|---|
| Spatial bin size | 4 px | UI spinner |
| Activity threshold (Otsu) | automatic | `backend.py` |
| HDBSCAN `min_cluster_size` | `max(3, N // 15)` | `backend.py` |
| NCC max lag | 25 % of period | `backend.py` |
| Louvain resolution | 1.0 | `backend.py` |
| SimCLR epochs | 100 | UI spinner |
| SimCLR batch size | `min(N, 256)` | `backend.py` |
| NT-Xent temperature τ | 0.5 | `backend.py` |
| Adam learning rate | 3 × 10⁻⁴ | `backend.py` |

---

## Dependencies

| Library | Purpose |
|---|---|
| `napari` | Viewer and layer management |
| `torch` | SimCLR 1-D CNN encoder training |
| `hdbscan` | Density-based clustering |
| `python-louvain` (`community`) | Louvain community detection on graphs |
| `networkx` | Graph construction |
| `scikit-learn` | Feature scaling, spectral clustering fallback |
| `scipy` | Signal processing, NCC, beat detection |
| `scikit-image` | Spatial binning, Otsu threshold |
| `tifffile` | TIFF stack loading |
| `matplotlib` | Trace plots in the UI |

---

## License

MIT
