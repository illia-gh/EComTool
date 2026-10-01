"""Strict tariff-input boundary shared by Stage-1 and focused tests."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


PROFILE_LENGTH = 35_040


def require_positive_number(value, cell_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{cell_name} must contain a numeric value greater than zero") from exc
    if not np.isfinite(number) or number <= 0:
        raise ValueError(f"{cell_name} must contain a finite numeric value greater than zero")
    return number


def read_tariff_profile(path: str | Path) -> list[float]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Tariff file is missing: {path}")
    try:
        values = pd.read_excel(path, usecols=[0], skiprows=1, header=None)
    except Exception as exc:
        raise ValueError(f"Cannot read tariff workbook {path}: {exc}") from exc
    if values.shape != (PROFILE_LENGTH, 1):
        raise ValueError(
            f"Tariff file {path} must contain exactly {PROFILE_LENGTH} values "
            f"in A2:A{PROFILE_LENGTH + 1}; found {values.shape[0]}"
        )
    profile = pd.to_numeric(values.iloc[:, 0], errors="coerce").to_numpy(dtype=float)
    invalid = np.flatnonzero(~np.isfinite(profile))
    if invalid.size:
        excel_row = int(invalid[0]) + 2
        raise ValueError(f"Tariff file {path} contains a non-numeric value at A{excel_row}")
    return profile.tolist()
