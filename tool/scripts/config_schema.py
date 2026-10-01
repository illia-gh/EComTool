"""Schema + validator for EComTool model configs (ARB and SC).

The config is the JSON the UI writes and `loadConfig.m` (and later the Python
compute port) reads. This module is the single source of truth for which keys
exist, their types, and their valid ranges — used by the admin's step-3
validation gate and by anyone editing a config by hand.

Keys here mirror the defaults in `RawData/EComTool/MATLAB Part/*/loadConfig.m`
and `tool/defaults/{arb,sc}_default.json` exactly.

CLI:
    python tool/scripts/config_schema.py tool/defaults/arb_default.json --mode arb
    python tool/scripts/config_schema.py tool/defaults/sc_default.json  --mode sc
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Spec:
    kind: str                          # "float" | "int" | "bool" | "enum"
    lo: Optional[float] = None         # inclusive lower bound (float/int)
    hi: Optional[float] = None         # inclusive upper bound (float/int)
    choices: Optional[tuple] = None    # for enum
    desc: str = ""
    ui: bool = True                    # False: fixed model setting, hidden in UI


# --- Shared economic block (identical for ARB and SC) ---
SHARED: dict[str, Spec] = {
    "dso_UA":     Spec("float", 0, 5, desc="Ukraine grid distribution component, EUR/kWh"),
    "dso_LV":     Spec("float", 0, 5, desc="Latvia grid distribution component, EUR/kWh"),
    "trade":      Spec("float", 0, 5, desc="trade component, EUR/kWh"),
    "dso_patst":  Spec("float", 0, 5, desc="extra grid component, EUR/kWh"),

    "C_rate":  Spec("float", 0, 5, desc="battery C-rate (power/energy)"),
    "DoD":     Spec("float", 0, 1, desc="depth of discharge (0..1)"),
    "eta_ch":  Spec("float", 0, 1, desc="charge efficiency (>0..1)"),
    "eta_dis": Spec("float", 0, 1, desc="discharge efficiency (>0..1)"),

    # Input workbook has no BESS adoption cell; new BESS is installed in Year 1.
    "BESS_adoption": Spec("enum", choices=("Instantaneous", "Linear", "Exponential"),
                          desc="BESS adoption curve", ui=False),

    "batt_cost_EUR_per_kWh": Spec("float", 0, 1e5, desc="BESS CAPEX, EUR/kWh"),
    "bess_inv_cost_EUR":     Spec("float", 0, 1e7, desc="BESS fixed CAPEX, EUR"),
    "bess_opex_rate":        Spec("float", 0, 1, desc="BESS OPEX, fraction/yr"),
    "bess_replace_year":     Spec("int", 0, 20, desc="BESS replacement year (0=off)"),
    "bess_replace_factor":   Spec("float", 0, 1, desc="BESS replacement cost factor"),

    "pv_inverter_ratio":               Spec("float", 0, 2, desc="inverter kW per PV kW"),
    "pv_cost_EUR_per_kW":              Spec("float", 0, 1e5, desc="PV panels CAPEX, EUR/kW"),
    "pv_installation_cost_EUR_per_kW": Spec("float", 0, 1e5, desc="PV install CAPEX, EUR/kW"),
    "pv_inv_cost_EUR":                 Spec("float", 0, 1e5, desc="inverter CAPEX, EUR/kW"),
    "pv_opex_rate":                    Spec("float", 0, 1, desc="PV OPEX, fraction/yr"),
    "pv_inv_replace_year":             Spec("int", 0, 20, desc="inverter replace year (0=off)"),
    "pv_inv_replace_cost_EUR":         Spec("float", 0, 1e5, desc="inverter replace cost, EUR/kW"),

    "discount_rate":            Spec("float", 0, 1, desc="discount rate (0..1)"),
    "grid_emission_kg_per_kWh": Spec("float", 0, 2, desc="grid CO2, kg/kWh"),
    "simulation_start_year":    Spec("int", 2026, 2100, desc="calendar year represented by Year 1"),
    "green_tariff_end_year":    Spec("int", 2000, 2100, desc="last calendar year using Ukraine Green Tariff"),
    "green_tariff_EUR_per_kWh": Spec("float", 0, 10, desc="Ukraine Green Tariff export rate, EUR/kWh"),

    "cycle_cost": Spec("float", 0, 1, desc="throughput penalty, EUR/kWh"),

    "phase_count":         Spec("int", 1, 3, desc="grid phases (1 or 3)"),
    "billing_start_month": Spec("int", 1, 12, desc="net-billing year start month"),
    "billing_start_day":   Spec("int", 1, 31, desc="net-billing year start day"),

    # --- PCC voltage screening; 3-phase 400 V, see
    #     docs/grid-screening-plan.md. Impedance preset comes from
    #     Input!C4 Area Type, not from config. ---
    "power_factor":   Spec("float", 0, 1, desc="assumed power factor for Q estimate (>0..1)"),
    "V_grid_pu":       Spec("float", 0.8, 1.2, desc="assumed upstream grid voltage, p.u."),
    "screening_years": Spec("int_list", 1, 100, desc="milestone years to screen PCC voltage"),
}

ARB_ONLY: dict[str, Spec] = {
    "max_cycles_per_day":                  Spec("float", 0, 10, desc="max equiv full cycles/day"),
    "arb_horizon_hours":                   Spec("int", 1, 168, desc="rolling optimisation horizon, h"),
    "arb_apply_hours":                     Spec("int", 24, 24, desc="applied window per roll; fixed at 24 h"),
    "arb_curtailment_penalty_EUR_per_kWh": Spec("float", 0, 10, desc="PV curtailment penalty"),
    "reset_soc_each_year":                 Spec("bool", desc="reset SoC at year boundary"),
}

SC_ONLY: dict[str, Spec] = {
    "w_export_SC":                  Spec("float", 0, 10, desc="export weight (SC objective)"),
    "w_curt_SC":                    Spec("float", 0, 10, desc="curtailment weight (SC objective)"),
    "sc_objective":                 Spec("enum", choices=("energy", "cost"), desc="SC objective"),
    "sc_use_cost":                  Spec("bool", desc="use cost in SC dispatch"),
    "fast_sc_dispatch":             Spec("bool", desc="one LP/day fast dispatch"),
}


def schema_for(mode: str) -> dict[str, Spec]:
    mode = mode.lower()
    if mode == "arb":
        return {**SHARED, **ARB_ONLY}
    if mode == "sc":
        return {**SHARED, **SC_ONLY}
    raise ValueError(f"mode must be 'arb' or 'sc', got {mode!r}")


def ui_schema(mode: str) -> dict[str, Spec]:
    """Fields shown under Advanced model parameters."""
    return {k: s for k, s in schema_for(mode).items() if s.ui}


def _check_one(key: str, val: Any, spec: Spec) -> Optional[str]:
    if spec.kind == "bool":
        if not isinstance(val, bool):
            return f"{key}: expected boolean, got {type(val).__name__}"
        return None
    if spec.kind == "enum":
        if val not in spec.choices:
            return f"{key}: {val!r} not in {list(spec.choices)}"
        return None
    if spec.kind == "int_list":
        if not isinstance(val, list) or not val:
            return f"{key}: expected a non-empty list of integers"
        for v in val:
            if isinstance(v, bool) or not isinstance(v, int):
                return f"{key}: expected list of integers, got {v!r}"
            if spec.lo is not None and v < spec.lo:
                return f"{key}: {v} below minimum {spec.lo}"
            if spec.hi is not None and v > spec.hi:
                return f"{key}: {v} above maximum {spec.hi}"
        return None
    # numeric
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return f"{key}: expected number, got {type(val).__name__}"
    numeric = float(val)
    if not math.isfinite(numeric):
        return f"{key}: expected finite number, got {val!r}"
    if key in {
        "power_factor", "C_rate", "eta_ch", "eta_dis",
        "green_tariff_EUR_per_kWh",
    } and numeric <= 0:
        return f"{key}: must be > 0"
    if spec.kind == "int" and numeric != int(numeric):
        return f"{key}: expected integer, got {val}"
    if spec.lo is not None and val < spec.lo:
        return f"{key}: {val} below minimum {spec.lo}"
    if spec.hi is not None and val > spec.hi:
        return f"{key}: {val} above maximum {spec.hi}"
    return None


def validate(cfg: dict, mode: str) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Empty errors == valid config."""
    schema = schema_for(mode)
    errors: list[str] = []
    warnings: list[str] = []

    for key, val in cfg.items():
        spec = schema.get(key)
        if spec is None:
            warnings.append(f"{key}: unknown key for mode '{mode}' (ignored by model)")
            continue
        msg = _check_one(key, val, spec)
        if msg:
            errors.append(msg)

    # cross-field checks
    if mode.lower() == "arb":
        h, a = cfg.get("arb_horizon_hours"), cfg.get("arb_apply_hours")
        if isinstance(h, (int, float)) and isinstance(a, (int, float)) and a > h:
            errors.append(f"arb_apply_hours ({a}) must be <= arb_horizon_hours ({h})")

    start_year = cfg.get("simulation_start_year")
    green_end_year = cfg.get("green_tariff_end_year")
    if isinstance(start_year, int) and isinstance(green_end_year, int) and green_end_year < start_year:
        warnings.append(
            "green_tariff_end_year is before simulation_start_year; Green Tariff fallback applies from Year 1"
        )

    return errors, warnings


def _constraint(spec: Spec) -> str:
    if spec.kind == "bool":
        return "true/false"
    if spec.kind == "enum":
        return "one of " + "/".join(str(c) for c in (spec.choices or ()))
    if spec.kind == "int_list":
        lo = "-inf" if spec.lo is None else spec.lo
        hi = "+inf" if spec.hi is None else spec.hi
        return f"list of int in [{lo}, {hi}]"
    rng = spec.kind
    if spec.lo is not None or spec.hi is not None:
        lo = "-inf" if spec.lo is None else spec.lo
        hi = "+inf" if spec.hi is None else spec.hi
        rng += f" in [{lo}, {hi}]"
    return rng


def describe_checks(cfg: dict, mode: str) -> list[str]:
    """Human-readable log of what the validator inspected, for the UI step-3 panel."""
    schema = schema_for(mode)
    lines = [f"Validating {mode.upper()} config - {len(cfg)} value(s) against schema"]
    known = 0
    for key, val in sorted(cfg.items()):
        spec = schema.get(key)
        if spec is None:
            lines.append(f"  ? {key} = {val}  (unknown key for {mode}, ignored by model)")
            continue
        known += 1
        msg = _check_one(key, val, spec)
        mark = "ok" if msg is None else "FAIL"
        detail = _constraint(spec) + (f" - {spec.desc}" if spec.desc else "")
        lines.append(f"  [{mark}] {key} = {val}  ({detail})")
        if msg:
            lines.append(f"        -> {msg}")
    if mode.lower() == "arb":
        h, a = cfg.get("arb_horizon_hours"), cfg.get("arb_apply_hours")
        if isinstance(h, (int, float)) and isinstance(a, (int, float)):
            ok = a <= h
            lines.append(f"  [{'ok' if ok else 'FAIL'}] cross-check: "
                         f"arb_apply_hours ({a}) <= arb_horizon_hours ({h})")
    lines.append(f"{known} known parameter(s) checked.")
    return lines


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("config", help="path to a config JSON")
    p.add_argument("--mode", choices=["arb", "sc"], help="model mode (inferred from filename if omitted)")
    args = p.parse_args()

    path = Path(args.config)
    mode = args.mode or ("sc" if "sc" in path.stem.lower() else "arb")
    cfg = json.loads(path.read_text(encoding="utf-8"))

    errors, warnings = validate(cfg, mode)
    for w in warnings:
        print(f"  WARN  {w}")
    for e in errors:
        print(f"  ERROR {e}")
    if errors:
        print(f"INVALID ({len(errors)} error(s)) - {path.name} [{mode}]")
        return 1
    print(f"OK - {path.name} [{mode}], {len(cfg)} keys valid"
          + (f", {len(warnings)} warning(s)" if warnings else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
