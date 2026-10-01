"""SC-mode battery dispatch, ported from dispatch_day_sc_fast.m + simulate_20y.m.

Fast rule-based self-consumption dispatch (the default `fast_sc_dispatch=true`):
charge only from PV surplus, discharge only to cover load deficit, never
discharge to grid, no curtailment. SOC carries across days and years (clamped
to each year's usable band); it is not reset per year.
"""
from __future__ import annotations

import numpy as np

DT_H = 0.25
STEPS_PER_DAY = 96
DAYS_PER_YEAR = 365


def _make_batt(cap_kWh: float, power_kW: float, DoD: float, eta_ch: float, eta_dis: float) -> dict:
    Emax = cap_kWh
    Emin = (1 - DoD) * cap_kWh
    return {"Emax": Emax, "Emin": Emin, "SOC0": 0.5 * (Emax + Emin),
            "Pch": power_kW, "Pdis": power_kW, "eta_ch": eta_ch, "eta_dis": eta_dis}


def dispatch_day(pv_day_kW: np.ndarray, load_kWh_day: np.ndarray, batt: dict,
                 soc_start: float) -> dict:
    Epv = pv_day_kW * DT_H            # kW -> kWh/step
    Eload = load_kWh_day             # already kWh/step
    Edef = np.maximum(0.0, Eload - Epv)
    Esur = np.maximum(0.0, Epv - Eload)
    Ech_max = batt["Pch"] * DT_H
    Edis_max = batt["Pdis"] * DT_H
    eta_ch, eta_dis = batt["eta_ch"], batt["eta_dis"]
    Emax, Emin = batt["Emax"], batt["Emin"]

    T = len(pv_day_kW)
    Eimp = np.zeros(T); Eexp = np.zeros(T); Ech = np.zeros(T); Edis = np.zeros(T)
    soc = min(max(soc_start, Emin), Emax)
    for t in range(T):
        max_ch = max(0.0, (Emax - soc) / eta_ch)
        ch = min(Esur[t], Ech_max, max_ch)
        soc += eta_ch * ch
        max_dis = max(0.0, (soc - Emin) * eta_dis)
        dis = min(Edef[t], Edis_max, max_dis)
        soc -= dis / eta_dis
        Ech[t] = ch; Edis[t] = dis
        Eexp[t] = Esur[t] - ch
        Eimp[t] = Edef[t] - dis
    return {"Eimp": Eimp, "Eexp": Eexp, "Ech": Ech, "Edis": Edis, "soc_end": soc}


def simulate_sc_fast(load_kWh: np.ndarray, pv_kW: np.ndarray, cap_y: np.ndarray,
                     power_y: np.ndarray, DoD: float, eta_ch: float, eta_dis: float,
                     buy: np.ndarray, sell: np.ndarray, month: np.ndarray,
                     reset_soc_years: np.ndarray | None = None) -> dict:
    """Run the fast SC dispatch over [Y, N]; return per-year aggregates + bill.

    Monthly net-billing wallet (simulate_20y.m): within each year credit
    accumulates month by month, offsets that month's import cost (bill >= 0),
    and the leftover wallet burns at year end. load/pv are billing-shifted;
    day d spans steps [d*96, d*96+96); `month` is the per-step billing month.
    """
    Y, N = load_kWh.shape
    if reset_soc_years is None:
        reset_soc_years = np.zeros(Y, dtype=bool)
    else:
        reset_soc_years = np.asarray(reset_soc_years, dtype=bool)
        if reset_soc_years.shape != (Y,):
            raise ValueError(f"reset_soc_years must have shape {(Y,)}")
    batts = [_make_batt(cap_y[y], power_y[y], DoD, eta_ch, eta_dis) for y in range(Y)]
    day_month = month[np.arange(DAYS_PER_YEAR) * STEPS_PER_DAY] - 1   # 0..11 per day

    yImport = np.zeros(Y); yExport = np.zeros(Y)
    yCharge = np.zeros(Y); yDischarge = np.zeros(Y)
    yLoad = np.zeros(Y); yPV = np.zeros(Y)
    yBill = np.zeros(Y); yImportCost = np.zeros(Y)
    yCredit = np.zeros(Y); yWalletBurn = np.zeros(Y)
    ySOCStart = np.zeros(Y)
    # Peak net grid power at PCC (kW) for grid-constraint screening: max over the
    # year of per-step import/export energy converted to power (/ DT_H).
    yPeakImport = np.zeros(Y); yPeakExport = np.zeros(Y)

    soc = batts[0]["SOC0"]
    for y in range(Y):
        b = batts[y]
        soc = (b["SOC0"] if reset_soc_years[y]
               else min(max(soc, b["Emin"]), b["Emax"]))
        ySOCStart[y] = soc
        yLoad[y] = load_kWh[y].sum()
        yPV[y] = pv_kW[y].sum() * DT_H
        import_cost_m = np.zeros(12)
        credit_m = np.zeros(12)
        for d in range(DAYS_PER_YEAR):
            k0 = d * STEPS_PER_DAY
            k1 = k0 + STEPS_PER_DAY
            out = dispatch_day(pv_kW[y, k0:k1], load_kWh[y, k0:k1], b, soc)
            soc = out["soc_end"]
            yImport[y] += out["Eimp"].sum()
            yExport[y] += out["Eexp"].sum()
            yCharge[y] += out["Ech"].sum()
            yDischarge[y] += out["Edis"].sum()
            yPeakImport[y] = max(yPeakImport[y], out["Eimp"].max() / DT_H)
            yPeakExport[y] = max(yPeakExport[y], out["Eexp"].max() / DT_H)
            m = day_month[d]
            import_cost_m[m] += (buy[y, k0:k1] * out["Eimp"]).sum()
            credit_m[m] += (sell[y, k0:k1] * out["Eexp"]).sum()

        wallet = 0.0
        bill = 0.0
        for m in range(12):
            wallet += credit_m[m]
            used = min(wallet, import_cost_m[m])
            bill += import_cost_m[m] - used
            wallet -= used
        yBill[y] = bill
        yImportCost[y] = import_cost_m.sum()
        yCredit[y] = credit_m.sum()
        yWalletBurn[y] = wallet

    return {"yImport": yImport, "yExport": yExport, "yCharge": yCharge,
            "yDischarge": yDischarge, "yLoad": yLoad, "yPV": yPV,
            "yBill": yBill, "yImportCost": yImportCost, "yCredit": yCredit,
            "yWalletBurn": yWalletBurn,
            "ySOCStart": ySOCStart,
            "yPeakImport": yPeakImport, "yPeakExport": yPeakExport}
