"""Standalone launcher for CTA SimCLR Enhanced.

Usage:
    python run_cta_simclr.py
"""

import sys
import os

# Ensure the repo root is on sys.path so `CTA_SimCLR` is importable as a package.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Suppress Qt geometry / DPI warnings that appear on Windows high-DPI displays.
os.environ.setdefault("QT_LOGGING_RULES", "*.debug=false;qt.qpa.*=false")

import napari
from CTA_SimCLR.widget import CalciumControls


def main():
    viewer = napari.Viewer(title="Calcium Transient Analyzer — SimCLR Enhanced")
    ctrl   = CalciumControls(viewer)
    viewer.window.add_dock_widget(ctrl, area='right', name='CTA Controls')
    napari.run()


if __name__ == '__main__':
    main()
