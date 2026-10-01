"""Deterministic front-end of the Stage-2 model, ported from main.m / main_ARB.m.

Reads EComTool_Output.xlsx + country-specific price forecast + config, then builds the same
[years x 35040] matrices MATLAB builds before dispatch: buy/sell prices, the
grid/existing/project/consumer load-energy profiles, the EV components, and the
billing-year-shifted time axis. Everything here is deterministic (no LP) and is
validated against the golden Results' Load_Profiles / Price_Annual /
Price_TimeSeries / BESS_Degradation sheets.

Conventions mirror MATLAB exactly:
  - N = 96 * 365 = 35040 fifteen-minute steps per year.
  - Total Load columns are kW -> multiply by dt_h (0.25) for kWh/step.
  - EV Aggregated columns are kW; convert to kWh/step on import.
  - Prices in the country forecast file are already EUR/kWh.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

STEPS_PER_DAY = 96
DAYS_PER_YEAR = 365
N = STEPS_PER_DAY * DAYS_PER_YEAR          # 35040
DT_H = 0.25
DATA_START_ROW = 2                          # 0-indexed; MATLAB dataStartRow=3 (1-indexed)


# --- config -----------------------------------------------------------------

def load_config(config_path: Path | None, defaults_path: Path) -> dict:
    """Built-in defaults (the *_default.json) overlaid with a runtime config.json.

    Mirrors loadConfig.m: unknown keys in the runtime file are ignored.
    """
    cfg = json.loads(Path(defaults_path).read_text(encoding="utf-8"))
    if config_path and Path(config_path).is_file():
        user = json.loads(Path(config_path).read_text(encoding="utf-8"))
        for k, v in user.items():
            if k in cfg:
                cfg[k] = v
    return cfg


# --- input reading ----------------------------------------------------------

_YEAR_RE = re.compile(r"Year\s*(\d+)", re.IGNORECASE)
AREA_TYPES = ("urban", "suburban", "rural")
_TARIFF_MODES = {
    "fixed": "Fixed",
    "own fixed": "Own Fixed",
    "dynamic": "Dynamic",
    "own dynamic": "Own Dynamic",
    "green tariff": "Green Tariff",
    "greentariff": "Green Tariff",
}


def canonical_tariff_mode(value: str) -> str:
    """Normalize spelling while preserving tariff-source semantics."""
    normalized = " ".join(str(value).strip().lower().split())
    try:
        return _TARIFF_MODES[normalized]
    except KeyError as exc:
        raise ValueError(
            "tariff mode must be Fixed/Dynamic, Own Fixed/Own Dynamic, "
            f"or Green Tariff, got {value!r}"
        ) from exc


def canonical_area_type(value) -> str:
    """Input!C4 Area Type -> grid-screening impedance preset key."""
    normalized = str(value).strip().lower()
    if normalized not in AREA_TYPES:
        raise ValueError(f"Input!C4 Area Type must be Urban, Suburban, or Rural, got {value!r}")
    return normalized


def require_finite(name: str, values) -> None:
    """Reject coerced blanks/text before NaNs can enter dispatch or economics."""
    array = np.asarray(values, dtype=float)
    invalid = np.argwhere(~np.isfinite(array))
    if invalid.size:
        first = tuple(int(i) for i in invalid[0])
        raise ValueError(f"{name} contains non-finite value at index {first}")


def _has(h: str, *words: str) -> bool:
    hl = h.lower()
    return all(w.lower() in hl for w in words)


def _year_cols(headers: list[str], *words: str) -> dict[int, int]:
    """Map year-number -> column index for headers matching all `words` + 'Year N'."""
    out: dict[int, int] = {}
    for c, h in enumerate(headers):
        if _has(h, *words) and "year" in h.lower():
            m = _YEAR_RE.search(h)
            if m:
                out[int(m.group(1))] = c
    return out


@dataclass(frozen=True)
class Inputs:
    years: tuple[int, ...]
    pv_existing: np.ndarray   # [Y, N] kW; converted to kWh/step during dispatch
    pv_new: np.ndarray        # [Y, N] kW
    ev: np.ndarray            # [Y, N] project EV, kWh/step
    ev_baseline: np.ndarray   # [N] existing EV, kWh/step
    total_load_kW: np.ndarray  # [Y, N] kW
    from_grid_tariff: str
    to_grid_tariff: str
    from_grid_fixed: float
    to_grid_fixed: float
    pv_existing_kW: float
    pv_new_kW: float
    bess_existing_kWh: float
    bess_new_kWh: float
    pv_adoption: str
    country: str = ""
    to_grid_own_fixed_EUR_per_kWh: float = 0.0
    from_grid_profile: np.ndarray | None = None
    to_grid_profile: np.ndarray | None = None
    bess_degradation_rate: float = 0.02
    area_type: str = "suburban"  # Input!C4; inputs_from_frame always sets it

    def __post_init__(self) -> None:
        object.__setattr__(self, "years", tuple(int(year) for year in self.years))
        for name in ("pv_existing", "pv_new", "ev", "ev_baseline", "total_load_kW"):
            values = np.array(getattr(self, name), dtype=float, copy=True)
            values.setflags(write=False)
            object.__setattr__(self, name, values)
        for name in ("from_grid_profile", "to_grid_profile"):
            source = getattr(self, name)
            if source is not None:
                values = np.array(source, dtype=float, copy=True).reshape(-1)
                values.setflags(write=False)
                object.__setattr__(self, name, values)
        degradation_rate = float(self.bess_degradation_rate)
        if not np.isfinite(degradation_rate) or not 0 <= degradation_rate < 1:
            raise ValueError("BESS degradation rate must be finite and in [0, 1)")
        object.__setattr__(self, "bess_degradation_rate", degradation_rate)


def _excel_float_roundtrip(values: np.ndarray) -> np.ndarray:
    """Reproduce openpyxl's historical ``%.16g`` numeric serialization."""
    flat = np.asarray(values, dtype=float).reshape(-1)
    normalized = np.fromiter(
        (float("%.16g" % value) for value in flat),
        dtype=float,
        count=flat.size,
    )
    return normalized.reshape(np.shape(values))


def inputs_from_frame(raw: pd.DataFrame, *, normalize_excel: bool = False) -> Inputs:
    """Build immutable Stage-2 inputs from Stage-1's canonical table."""
    headers = [str(x) for x in raw.iloc[0].tolist()]

    def col_series(c: int) -> np.ndarray:
        values = pd.to_numeric(raw.iloc[DATA_START_ROW:, c], errors="coerce").to_numpy(float)
        return _excel_float_roundtrip(values) if normalize_excel else values

    def find_col(*words: str) -> int:
        for c, h in enumerate(headers):
            if _has(h, *words):
                return c
        raise ValueError(f"column matching {words} not found")

    # Other Input Data column, rows 2..7 (1-indexed) = 0-indexed 1..6
    other_c = next(c for c, h in enumerate(headers) if h.strip() == "Other Input Data")
    def other(row0: int) -> str:
        return str(raw.iloc[row0, other_c]).strip()
    area_type = canonical_area_type(other(1))
    capacities = np.array([
        float(other(2).replace(",", ".")),
        float(other(3).replace(",", ".")),
        float(other(4).replace(",", ".")),
        float(other(5).replace(",", ".")),
    ])
    if normalize_excel:
        capacities = _excel_float_roundtrip(capacities)
    pv_existing_kW, pv_new_kW, bess_existing_kWh, bess_new_kWh = capacities
    pv_adoption = other(6)

    metadata: dict = {}
    metadata_c = next(
        (c for c, h in enumerate(headers) if h.strip() == "Run Metadata"),
        None,
    )
    if metadata_c is not None:
        raw_metadata = raw.iloc[1, metadata_c]
        if pd.notna(raw_metadata) and str(raw_metadata).strip():
            try:
                metadata = json.loads(str(raw_metadata))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Run Metadata must contain valid JSON") from exc

    bess_input_unit = str(metadata.get("bess_input_unit", "kWh")).strip()
    if bess_input_unit.casefold() != "kwh":
        raise ValueError(
            "Run Metadata bess_input_unit must be 'kWh'; "
            f"got {bess_input_unit!r}"
        )
    # New Stage-1 output carries Input!B95 as a fraction. Historical Stage-1
    # workbooks predate this metadata and retain the former 2% assumption.
    bess_degradation_rate = float(metadata.get("bess_degradation_rate", 0.02))
    require_finite("BESS degradation rate", [bess_degradation_rate])
    if not 0 <= bess_degradation_rate < 1:
        raise ValueError("Run Metadata bess_degradation_rate must be in [0, 1)")

    from_c = find_col("From Grid", "EUR/kWh")
    to_c = find_col("To Grid", "EUR/kWh")
    from_grid_tariff = canonical_tariff_mode(raw.iloc[1, from_c])
    to_grid_tariff = canonical_tariff_mode(raw.iloc[1, to_c])
    from_grid_profile = col_series(from_c)
    to_grid_profile = col_series(to_c)
    # fixed tariff = first data value of the column (fixedTariffToMatrix.m)
    from_grid_fixed = float(from_grid_profile[0])
    to_grid_fixed = float(to_grid_profile[0])

    pv_base_cols = _year_cols(headers, "PV", "Aggregated", "Baseline")
    pv_new_cols = _year_cols(headers, "PV", "Aggregated", "New")
    ev_cols = _year_cols(headers, "EV", "Aggregated")
    tl_cols = _year_cols(headers, "Total Load")
    ev_baseline_c = next(c for c, h in enumerate(headers)
                         if h.strip().lower() == "ev aggregated baseline")

    years = sorted(pv_base_cols)
    Y = len(years)

    pv_existing = np.vstack([col_series(pv_base_cols[y]) for y in years])
    pv_new = np.vstack([col_series(pv_new_cols[y]) for y in years])
    # Stage-1 preserves EV power in kW. Stage-2 uses interval energy throughout.
    ev = np.vstack([col_series(ev_cols[y]) for y in years]) * DT_H
    total_load_kW = np.vstack([col_series(tl_cols[y]) for y in years])
    ev_baseline = col_series(ev_baseline_c) * DT_H

    for name, arr in [("pv_existing", pv_existing), ("pv_new", pv_new),
                      ("ev", ev), ("total_load", total_load_kW)]:
        if arr.shape != (Y, N):
            raise ValueError(f"{name} shape {arr.shape} != {(Y, N)}")
    if ev_baseline.shape != (N,):
        raise ValueError(f"ev_baseline shape {ev_baseline.shape} != {(N,)}")
    for name, arr in [("pv_existing", pv_existing), ("pv_new", pv_new),
                      ("ev", ev), ("total_load", total_load_kW),
                      ("ev_baseline", ev_baseline)]:
        require_finite(name, arr)
    require_finite(
        "installed capacities",
        [pv_existing_kW, pv_new_kW, bess_existing_kWh, bess_new_kWh],
    )
    if min(pv_existing_kW, pv_new_kW, bess_existing_kWh, bess_new_kWh) < 0:
        raise ValueError("installed capacities must be non-negative")
    if from_grid_tariff in {"Fixed", "Own Fixed"}:
        require_finite("From Grid fixed tariff", [from_grid_fixed])
    if to_grid_tariff in {"Fixed", "Own Fixed"}:
        require_finite("To Grid fixed tariff", [to_grid_fixed])
    if from_grid_tariff in {"Dynamic", "Own Dynamic"}:
        if from_grid_profile.shape != (N,):
            raise ValueError(f"From Grid tariff profile shape {from_grid_profile.shape} != {(N,)}")
        require_finite("From Grid tariff profile", from_grid_profile)
    if to_grid_tariff in {"Dynamic", "Own Dynamic"}:
        if to_grid_profile.shape != (N,):
            raise ValueError(f"To Grid tariff profile shape {to_grid_profile.shape} != {(N,)}")
        require_finite("To Grid tariff profile", to_grid_profile)

    country = str(metadata.get("country", "")).strip()
    if to_grid_tariff == "Green Tariff" and not country:
        # Legacy Stage-1 workbooks exposed Green Tariff only for Ukraine.
        country = "Ukraine"
    own_fixed_to = float(metadata.get("to_grid_own_fixed_EUR_per_kWh", 0.0) or 0.0)
    require_finite("To Grid own fixed fallback tariff", [own_fixed_to])
    if own_fixed_to <= 0:
        raise ValueError("To Grid own fixed fallback tariff (B92) must be greater than zero")

    return Inputs(
        tuple(years), pv_existing, pv_new, ev, ev_baseline, total_load_kW,
        from_grid_tariff, to_grid_tariff, from_grid_fixed, to_grid_fixed,
        pv_existing_kW, pv_new_kW, bess_existing_kWh, bess_new_kWh,
        pv_adoption, country, own_fixed_to,
        from_grid_profile, to_grid_profile, bess_degradation_rate, area_type,
    )


def read_output(path: Path) -> Inputs:
    """Legacy XLSX boundary for MATLAB and separate CLI workflows."""
    raw = pd.read_excel(path, header=None, engine="openpyxl")
    return inputs_from_frame(raw)


@lru_cache(maxsize=4)
def _read_price_matrix(path: str, mtime_ns: int, size: int) -> np.ndarray:
    """Read one immutable workbook matrix; file metadata invalidates process cache."""
    del mtime_ns, size
    matrix = pd.read_excel(path, header=None, engine="openpyxl").to_numpy(float)
    matrix.setflags(write=False)
    return matrix


def read_prices(path: Path, years: list[int]) -> np.ndarray:
    """Return spot prices [Y, N] in EUR/kWh from an [N x years] workbook."""
    path = Path(path).resolve()
    stat = path.stat()
    m = _read_price_matrix(str(path), stat.st_mtime_ns, stat.st_size)  # [N, cols]
    Y = len(years)
    spot = m[:, :Y].T   # [Y, N] EUR/kWh
    if spot.shape != (Y, N):
        raise ValueError(f"price shape {spot.shape} != {(Y, N)}")
    require_finite("spot prices", spot)
    return spot


# --- billing-year shift -----------------------------------------------------

def billing_start_index(start_month: int, start_day: int) -> int:
    """0-indexed step of (start_month, start_day) 00:00 in a 2021 Jan-1 axis."""
    ts = pd.Timestamp(2021, 1, 1) + pd.to_timedelta(np.arange(N) * 15, unit="m")
    mask = (ts.month == start_month) & (ts.day == start_day) & (ts.hour == 0) & (ts.minute == 0)
    idx = np.flatnonzero(np.asarray(mask))
    if idx.size == 0:
        raise ValueError(f"start date {start_month:02d}-{start_day:02d} not found")
    return int(idx[0])


def shifted_order(i0: int) -> np.ndarray:
    return np.concatenate([np.arange(i0, N), np.arange(0, i0)])


# --- assembled state --------------------------------------------------------

# --- adoption + BESS capacity/degradation (deterministic, no dispatch) -------

def adoption_curve(Y: int, mode: str, years_to_full: int | None = None) -> np.ndarray:
    """Port of build_adoption_curve.m: cumulative adoption share in [0, 1]."""
    if not years_to_full:
        years_to_full = Y
    years_to_full = max(1, min(Y, round(years_to_full)))
    mode = mode.strip().lower()
    t = np.arange(1, Y + 1, dtype=float)
    if mode == "instantaneous":
        f = np.ones(Y)
    elif mode == "linear":
        f = np.minimum(t / years_to_full, 1.0)
    elif mode == "exponential":
        k = 3.0
        x = np.minimum(t / years_to_full, 1.0)
        f = (np.exp(k * x) - 1.0) / (np.exp(k) - 1.0)
        f[t >= years_to_full] = 1.0
    else:
        raise ValueError(f"unknown adoption mode: {mode}")
    f = np.clip(f, 0.0, 1.0)
    return np.maximum.accumulate(f)   # enforce monotonicity


def _degraded_existing(W: float, rate: float, Y: int) -> np.ndarray:
    return W * (1 - rate) ** np.arange(Y)


def _degraded_adoption(
        W: float, f: np.ndarray, rate: float,
        replacement_year: int = 0, replacement_enabled: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """New-BESS effective capacity by installation cohort (degraded_adoption_capacity.m)."""
    Y = len(f)
    df = np.concatenate([[f[0]], np.diff(f)])
    if np.any(df < -1e-9):
        raise ValueError("BESS adoption curve must be non-decreasing")
    cap = np.zeros(Y)
    replacement_years = np.zeros(Y, dtype=bool)
    for y0 in range(Y):
        cohort = W * max(df[y0], 0.0)
        if cohort <= 1e-12:
            continue
        for y in range(y0, Y):
            age = y - y0
            if replacement_enabled and age >= replacement_year:
                age -= replacement_year
            cap[y] += cohort * (1 - rate) ** age
        replacement_index = y0 + replacement_year
        if replacement_enabled and replacement_index < Y:
            replacement_years[replacement_index] = True
    return cap, replacement_years


def bess_capacity(inp: "Inputs", cfg: dict) -> dict:
    """Build per-year capacity from input kWh and derive power from C-rate."""
    Y = len(inp.years)
    rate = inp.bess_degradation_rate
    c_rate = float(cfg["C_rate"])
    if not np.isfinite(c_rate) or c_rate <= 0:
        raise ValueError("C_rate must be finite and greater than zero")
    We = float(inp.bess_existing_kWh)
    Wn = float(inp.bess_new_kWh)
    require_finite("BESS installed capacity", [We, Wn])
    if We < 0 or Wn < 0:
        raise ValueError("BESS installed capacity must be non-negative")
    P_existing = c_rate * We
    P_new = c_rate * Wn
    fB = adoption_curve(Y, cfg["BESS_adoption"], Y)   # BESS_adoption_years = S.years

    nominal = We * np.ones(Y) + Wn * fB
    eff_existing = _degraded_existing(We, rate, Y)
    replacement_year = int(cfg["bess_replace_year"])
    replacement_enabled = replacement_year > 0 and cfg["bess_replace_factor"] > 0
    eff_new, replacement_years = _degraded_adoption(
        Wn, fB, rate, replacement_year, replacement_enabled,
    )
    eff_total = eff_existing + eff_new
    power_existing = np.full(Y, P_existing)
    power = power_existing + P_new * fB
    return {
        "adoption": fB, "nominal": nominal,
        "existing_nominal": We, "new_nominal": Wn,
        "eff_existing": eff_existing, "eff_new": eff_new, "eff_total": eff_total,
        "power_existing": power_existing, "power": power,
        "replacement_years": replacement_years,
    }


@dataclass
class State:
    years: list[int]
    cfg: dict
    inp: Inputs
    buy: np.ndarray            # [Y, N] EUR/kWh, billing-shifted
    sell: np.ndarray           # [Y, N]
    load_grid: np.ndarray      # [Y, N] kWh/step, shifted
    load_existing: np.ndarray
    load_project: np.ndarray
    load_consumers: np.ndarray  # NOT shifted (matches MATLAB), rotation-invariant sums
    ev_baseline_y: np.ndarray  # [Y, N] kWh/step (each year identical), not shifted
    ev_project_y: np.ndarray   # [Y, N]
    pv_existing: np.ndarray    # [Y, N] shifted
    pv_new: np.ndarray
    pv_project: np.ndarray
    idx_day: np.ndarray        # [N] billing-order day 1..365
    idx_period: np.ndarray     # [N] 1..96
    idx_month: np.ndarray      # [N] 1..12 billing-order
    sell_ordinary: np.ndarray | None = None  # [Y, N], non-Green export price
    green_origin_tracking: bool = False


def build_state(inp: Inputs, spot: np.ndarray, cfg: dict) -> State:
    Y = len(inp.years)
    is_ukraine = inp.country.strip().casefold() in {"ukraine", "україна", "ua"}
    dso = cfg["dso_UA"] if is_ukraine else cfg["dso_LV"]
    trade, dso_patst = cfg["trade"], cfg["dso_patst"]

    def repeated_profile(values: np.ndarray | None, name: str) -> np.ndarray:
        if values is None:
            raise ValueError(f"{name} profile is missing")
        profile = np.asarray(values, dtype=float).reshape(-1)
        if profile.shape != (N,):
            raise ValueError(f"{name} profile shape {profile.shape} != {(N,)}")
        require_finite(f"{name} profile", profile)
        return np.tile(profile, (Y, 1))

    # --- prices (tariff selection, mirrors main.m) ---
    from_grid_tariff = canonical_tariff_mode(inp.from_grid_tariff)
    to_grid_tariff = canonical_tariff_mode(inp.to_grid_tariff)

    if from_grid_tariff == "Dynamic":
        buy = np.maximum(spot, 0) + dso + trade + dso_patst
    elif from_grid_tariff == "Own Dynamic":
        buy = np.maximum(repeated_profile(inp.from_grid_profile, "From Grid Own Dynamic"), 0)
    elif from_grid_tariff == "Fixed":
        buy = np.maximum(inp.from_grid_fixed, 0)
        if not is_ukraine:
            buy = buy + dso + trade + dso_patst
        buy = np.full((Y, N), buy, dtype=float)
    elif from_grid_tariff == "Own Fixed":
        buy = np.maximum(inp.from_grid_fixed, 0)
        buy = np.full((Y, N), buy, dtype=float)
    elif from_grid_tariff == "Green Tariff":
        raise ValueError("Green Tariff is valid only for To Grid exports")

    ordinary_sell = np.maximum(spot - trade, 0) + dso_patst
    green_origin_tracking = False
    if to_grid_tariff == "Dynamic":
        sell = ordinary_sell
    elif to_grid_tariff == "Own Dynamic":
        sell = np.maximum(repeated_profile(inp.to_grid_profile, "To Grid Own Dynamic"), 0)
    elif to_grid_tariff in {"Fixed", "Own Fixed"}:
        sell = np.full((Y, N), max(inp.to_grid_fixed, 0.0), dtype=float)
    elif to_grid_tariff == "Green Tariff":
        if not is_ukraine:
            raise ValueError("Green Tariff is available only for Ukraine")
        start_year = int(cfg.get("simulation_start_year", 2026))
        green_end_year = int(cfg.get("green_tariff_end_year", 2029))
        green_rate = float(cfg.get("green_tariff_EUR_per_kWh", 0.10567))
        require_finite("Green Tariff rate", [green_rate])
        if green_rate <= 0:
            raise ValueError("Green Tariff rate must be greater than zero")
        own_fixed = float(inp.to_grid_own_fixed_EUR_per_kWh)
        require_finite("To Grid own fixed fallback tariff", [own_fixed])
        if own_fixed <= 0:
            raise ValueError("To Grid own fixed fallback tariff (B92) must be greater than zero")
        sell = np.empty((Y, N), dtype=float)
        green_origin_tracking = True
        for row, ordinal_year in enumerate(inp.years):
            calendar_year = start_year + int(ordinal_year) - 1
            if calendar_year <= green_end_year:
                sell[row] = green_rate
            else:
                sell[row] = own_fixed

    # --- load-energy components (kWh/step), pre-shift ---
    consumers = inp.total_load_kW * DT_H                       # [Y, N]
    ev_base_y = np.tile(inp.ev_baseline, (Y, 1))              # [Y, N] each year same
    load_grid = consumers + ev_base_y
    load_existing = consumers + ev_base_y
    load_project = consumers + inp.ev
    ev_project_y = inp.ev

    # --- billing-year shift (rotate columns) ---
    i0 = billing_start_index(cfg["billing_start_month"], cfg["billing_start_day"])
    order = shifted_order(i0)
    buy = buy[:, order]
    sell = sell[:, order]
    sell_ordinary = ordinary_sell[:, order]
    load_grid = load_grid[:, order]
    load_existing = load_existing[:, order]
    load_project = load_project[:, order]
    pv_existing = inp.pv_existing[:, order]
    pv_new = inp.pv_new[:, order]
    # consumers / ev components are NOT shifted in main.m (annual sums invariant)

    # --- billing-order index vectors ---
    k = np.arange(N)
    idx_period = (k % STEPS_PER_DAY) + 1
    idx_day = (k // STEPS_PER_DAY) + 1
    sm = cfg["billing_start_month"]
    ts_b = pd.Timestamp(2021, sm, cfg["billing_start_day"]) + pd.to_timedelta(k * 15, unit="m")
    idx_month = ((ts_b.month.to_numpy() - sm) % 12) + 1

    return State(
        years=inp.years, cfg=cfg, inp=inp, buy=buy, sell=sell,
        sell_ordinary=sell_ordinary, green_origin_tracking=green_origin_tracking,
        load_grid=load_grid, load_existing=load_existing, load_project=load_project,
        load_consumers=consumers, ev_baseline_y=ev_base_y, ev_project_y=ev_project_y,
        pv_existing=pv_existing, pv_new=pv_new, pv_project=pv_existing + pv_new,
        idx_day=idx_day, idx_period=idx_period, idx_month=idx_month,
    )
