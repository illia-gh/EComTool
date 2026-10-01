"""Economics: CAPEX, incremental cashflow, NPV. Ported from
build_cashflow_project_adoption_20y.m, calculate_new_project_capex, compute_npv.m.
"""
from __future__ import annotations

import numpy as np


def new_project_capex(cfg: dict, pv_new_kW: float, bess_new_kWh: float) -> dict:
    pv = (pv_new_kW * cfg["pv_cost_EUR_per_kW"]
          + pv_new_kW * cfg["pv_installation_cost_EUR_per_kW"]
          + pv_new_kW * cfg["pv_inverter_ratio"] * cfg["pv_inv_cost_EUR"])
    bess = bess_new_kWh * cfg["batt_cost_EUR_per_kWh"]
    if bess_new_kWh > 0:
        bess += cfg["bess_inv_cost_EUR"]
    return {"PV_new_EUR": pv, "BESS_new_EUR": bess, "Total_new_EUR": pv + bess}


def build_cashflow(cfg: dict, pv_new_kW: float, bess_new_kWh: float,
                   savings_year: np.ndarray, fPV: np.ndarray, fBESS: np.ndarray) -> np.ndarray:
    """Incremental cashflow [Y+1] for the new PV+BESS part (adoption cohorts)."""
    Y = len(savings_year)
    fPV = np.asarray(fPV, float); fBESS = np.asarray(fBESS, float)
    dPV_frac = np.concatenate([[fPV[0]], np.diff(fPV)])
    dBESS_frac = np.concatenate([[fBESS[0]], np.diff(fBESS)])
    dPV_new_kW = pv_new_kW * dPV_frac
    dBESS_new_kWh = bess_new_kWh * dBESS_frac
    pv_new_active_kW = pv_new_kW * fPV
    bess_new_active_kWh = bess_new_kWh * fBESS

    cash = np.zeros(Y + 1)

    # CAPEX tranches at start of year y -> cash[y-1] (0-indexed cash[y0] for MATLAB cash(y))
    bess_fixed_paid = False
    first_bess = next((y for y in range(Y) if dBESS_new_kWh[y] > 1e-12), None)
    for y in range(Y):
        capex = 0.0
        if dPV_new_kW[y] > 0:
            capex += (dPV_new_kW[y] * cfg["pv_cost_EUR_per_kW"]
                      + dPV_new_kW[y] * cfg["pv_installation_cost_EUR_per_kW"]
                      + dPV_new_kW[y] * cfg["pv_inverter_ratio"] * cfg["pv_inv_cost_EUR"])
        if dBESS_new_kWh[y] > 0:
            capex += dBESS_new_kWh[y] * cfg["batt_cost_EUR_per_kWh"]
            if not bess_fixed_paid:
                capex += cfg["bess_inv_cost_EUR"]
                bess_fixed_paid = True
        cash[y] -= capex   # MATLAB cash(y), y=1..Y -> index y-1

    # annual operating cashflows -> cash(y+1) => index y
    for y in range(Y):
        sav = savings_year[y]
        pv_opex_base = (pv_new_active_kW[y] * cfg["pv_cost_EUR_per_kW"]
                        + pv_new_active_kW[y] * cfg["pv_installation_cost_EUR_per_kW"]
                        + pv_new_active_kW[y] * cfg["pv_inverter_ratio"] * cfg["pv_inv_cost_EUR"])
        bess_opex_base = bess_new_active_kWh[y] * cfg["batt_cost_EUR_per_kWh"]
        if first_bess is not None and y >= first_bess:
            bess_opex_base += cfg["bess_inv_cost_EUR"]
        opex = cfg["pv_opex_rate"] * pv_opex_base + cfg["bess_opex_rate"] * bess_opex_base
        cash[y + 1] += sav - opex

    # cohort battery replacements
    if cfg["bess_replace_year"] > 0 and cfg["bess_replace_factor"] > 0:
        for y0 in range(Y):
            if dBESS_new_kWh[y0] <= 1e-12:
                continue
            idx = y0 + int(cfg["bess_replace_year"])   # MATLAB y0+repl -> 0-indexed cash idx
            if idx <= Y:
                cash[idx] -= cfg["bess_replace_factor"] * dBESS_new_kWh[y0] * cfg["batt_cost_EUR_per_kWh"]

    # cohort PV inverter replacements
    if cfg["pv_inv_replace_year"] > 0 and cfg["pv_inv_replace_cost_EUR"] > 0:
        for y0 in range(Y):
            if dPV_new_kW[y0] <= 1e-12:
                continue
            idx = y0 + int(cfg["pv_inv_replace_year"])
            if idx <= Y:
                cash[idx] -= dPV_new_kW[y0] * cfg["pv_inverter_ratio"] * cfg["pv_inv_replace_cost_EUR"]

    return cash


def compute_npv(cash: np.ndarray, r: float) -> tuple[float, np.ndarray]:
    cash = np.asarray(cash, float)
    disc = (1 + r) ** np.arange(len(cash))
    pv = cash / disc
    return float(pv.sum()), np.cumsum(pv)
