"""Shared paths / constants. Everything is relative to the repo or configurable.

Data directory (downloaded market data, built npz):  env NFB_DATA_DIR, default ./data
Every script also accepts --data-dir to override it.
"""
import os
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parent

# Data window that is downloaded / built (hourly grid starts at T0, ends before T1).
# T0 is ~4 weeks before the first reported window so funding / basis / volume / OI signals exist (and are causal) on day 1.
DATA_START = os.environ.get('NFB_DATA_START', '2025-10-01')
DATA_END = os.environ.get('NFB_DATA_END', '2026-10-28')     # exclusive
# Reported windows (start inclusive, end exclusive). 1y = headline, 6m = most recent half year.
WINDOWS = {"1y": ("2025-10-28", "2026-10-28"), "6m": ("2026-05-01", "2026-10-28")}

BINANCE_VISION = "https://data.binance.vision/data"


def data_dir(arg=None) -> Path:
    p = Path(arg or os.environ.get("NFB_DATA_DIR") or (REPO / "data")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_symbols(path=None) -> pd.DataFrame:
    """symbols_signal.csv (default pool, 267 perps): fut_symbol, spot_symbol, mult (perp contract multiplier, e.g. 1000XXXUSDT -> 1000)."""
    df = pd.read_csv(path or (REPO / "symbols_signal.csv"))
    df["mult"] = df["mult"].astype(float)
    return df
