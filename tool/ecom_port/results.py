"""Build all nine SC Results-schema sheets from the ported model state.

Column names and order match export_pv_bess_sc_results.m so downstream reports
and parity checks can consume MATLAB and Python output interchangeably.
"""
from __future__ import annotations


import numpy as np
import pandas as pd

from . import arb_dispatch as A
from . import economics as E
from . import grid_screening as GS
from .frontend import DT_H, N, Inputs, State, adoption_curve, bess_capacity
from .sc_dispatch import simulate_sc_fast


def _safe_div(a, b):
    a = np.asarray(a, float); b = np.asarray(b, float)
    return np.divide(a, b, out=np.zeros_like(a), where=b != 0)


def project_sim(s: State):
    """Run the SC project-scenario dispatch; returns per-year aggregates + bill."""
    cap = bess_capacity(s.inp, s.cfg)
    return simulate_sc_fast(
        s.load_project, s.pv_project, cap["eff_total"], cap["power"],
        s.cfg["DoD"], s.cfg["eta_ch"], s.cfg["eta_dis"],
        s.buy, s.sell, s.idx_month,
        cap["replacement_years"],
    )


def existing_sim(s: State):
    """Existing assets (existing PV + existing degraded BESS) at the BASELINE load."""
    cap = bess_capacity(s.inp, s.cfg)
    return simulate_sc_fast(
        s.load_existing, s.pv_existing, cap["eff_existing"], cap["power_existing"],
        s.cfg["DoD"], s.cfg["eta_ch"], s.cfg["eta_dis"],
        s.buy, s.sell, s.idx_month,
    )


def existing_sim_project_load(s: State):
    """Existing assets at the PROJECT load — the counterfactual baseline for NPV.

    Same energy assets as `existing_sim` but with the project EV load, so that the
    PV+BESS savings are measured at an identical load and are not contaminated by
    the cost of extra EV charging.
    """
    cap = bess_capacity(s.inp, s.cfg)
    return simulate_sc_fast(
        s.load_project, s.pv_existing, cap["eff_existing"], cap["power_existing"],
        s.cfg["DoD"], s.cfg["eta_ch"], s.cfg["eta_dis"],
        s.buy, s.sell, s.idx_month,
    )


def grid_bill_y(s: State, load: np.ndarray | None = None) -> np.ndarray:
    """Grid-only bill (no PV/BESS, no wallet): sum(buy * load) per year.

    Defaults to the baseline grid load; pass `load` for the project-load variant.
    """
    if load is None:
        load = s.load_grid
    return (s.buy * load).sum(axis=1)


def decompose_savings(exist_bill, exist_pl_bill, proj_bill) -> dict:
    """Split the original->project bill change into the two independent effects.

    Identity (holds exactly, residual ~ 0):
        combined = pv_bess_savings_same_load - ev_load_growth_cost
    where each bill is:
        exist_bill    : existing assets @ baseline load
        exist_pl_bill : existing assets @ project  load
        proj_bill     : project  assets @ project  load
    """
    savings = exist_pl_bill - proj_bill      # PV+BESS effect at equal (project) load
    ev_growth = exist_pl_bill - exist_bill   # cost of the added EV charging
    combined = exist_bill - proj_bill        # total original -> project change
    residual = combined - (savings - ev_growth)
    return {"savings_same_load": savings, "ev_growth": ev_growth,
            "combined": combined, "residual": residual}


def run_scenarios(s: State) -> dict:
    """Run the scenarios + equal-load cashflow + NPV; returns a builder context.

    NPV is driven by the PV+BESS savings measured at the SAME (project) load, so
    the cost of extra EV charging no longer leaks into it (see the corrected
    MATLAB decomposition, docs/CORRECTION_EV_load.md).
    """
    # Current Python port implements MATLAB's fast_sc_dispatch=true branch.
    # LP-backed SC objectives remain MATLAB-only until explicitly ported.
    proj = project_sim(s)
    exist = existing_sim(s)                       # existing assets @ baseline load
    exist_pl = existing_sim_project_load(s)
    grid = grid_bill_y(s)                         # grid only @ baseline load
    grid_pl = grid_bill_y(s, s.load_project)
    Y = len(s.years)
    fPV = adoption_curve(Y, s.inp.pv_adoption, Y)
    fBESS = adoption_curve(Y, s.cfg["BESS_adoption"], Y)
    dec = decompose_savings(exist["yBill"], exist_pl["yBill"], proj["yBill"])
    cap = bess_capacity(s.inp, s.cfg)
    cash = E.build_cashflow(s.cfg, s.inp.pv_new_kW, cap["new_nominal"],
                            dec["savings_same_load"], fPV, fBESS)
    npv, npv_t = E.compute_npv(cash, s.cfg["discount_rate"])
    return {"proj": proj, "exist": exist, "exist_pl": exist_pl,
            "grid": grid, "grid_pl": grid_pl, **dec,
            "cash": cash, "npv": npv, "npv_t": npv_t}


def energy_balance_df(s: State, sim: dict) -> pd.DataFrame:
    pv_existing_y = s.pv_existing.sum(axis=1) * DT_H
    pv_new_y = s.pv_new.sum(axis=1) * DT_H
    pv_y = sim["yPV"]
    imp = sim["yImport"]; exp = sim["yExport"]
    curt = np.zeros(len(s.years)); charge = sim["yCharge"]; discharge = sim["yDischarge"]
    load_y = sim["yLoad"]

    pv_to_load = np.maximum(pv_y - charge - exp - curt, 0.0)
    pv_self_consumed = pv_to_load + charge
    self_suff = 1 - _safe_div(imp, load_y)
    self_suff = np.where(load_y <= 0, 0.0, self_suff)

    return pd.DataFrame({
        "Year": s.years,
        "Grid_only_baseline_load_kWh": s.load_grid.sum(axis=1),
        "Existing_baseline_load_kWh": s.load_existing.sum(axis=1),
        "Project_load_kWh": load_y,
        "PV_existing_generation_kWh": pv_existing_y,
        "PV_new_generation_kWh": pv_new_y,
        "PV_project_generation_kWh": pv_y,
        "Project_grid_import_kWh": imp,
        "Project_grid_export_kWh": exp,
        "Project_curtailment_kWh": curt,
        "Project_BESS_charge_kWh": charge,
        "Project_BESS_discharge_kWh": discharge,
        "Project_PV_self_consumed_kWh": pv_self_consumed,
        "Project_self_consumption_rate": _safe_div(pv_self_consumed, pv_y),
        "Project_self_sufficiency_rate": self_suff,
        "Project_export_rate": _safe_div(exp, pv_y),
        "Project_curtailment_rate": _safe_div(curt, pv_y),
    })


def economics_df(s: State, ctx: dict) -> pd.DataFrame:
    """SC Economics sheet — column names match the corrected export_pv_bess_sc_results.m.

    Every bill is labelled with both the assets and the load it was computed at, so
    the PV+BESS effect (equal load) is never confused with the EV-load-growth cost.
    """
    grid = ctx["grid"]; grid_pl = ctx["grid_pl"]
    exist = ctx["exist"]; exist_pl = ctx["exist_pl"]; proj = ctx["proj"]
    return pd.DataFrame({
        "Year": s.years,
        "Grid_bill_baseline_load_EUR": grid,
        "Grid_bill_project_load_EUR": grid_pl,
        "Existing_assets_bill_baseline_load_EUR": exist["yBill"],
        "Existing_assets_bill_project_load_EUR": exist_pl["yBill"],
        "Project_assets_bill_project_load_EUR": proj["yBill"],
        # Technology comparisons, each at a single consistent load.
        "Existing_assets_savings_vs_grid_baseline_load_EUR": grid - exist["yBill"],
        "Existing_assets_savings_vs_grid_project_load_EUR": grid_pl - exist_pl["yBill"],
        "Project_assets_savings_vs_grid_project_load_EUR": grid_pl - proj["yBill"],
        # Causal decomposition: PV+BESS effect vs EV-load-growth cost.
        "EV_load_growth_cost_existing_assets_EUR": ctx["ev_growth"],
        "EV_load_growth_cost_grid_only_EUR": grid_pl - grid,
        "PV_BESS_savings_same_project_load_EUR": ctx["savings_same_load"],
        "Combined_bill_change_original_to_project_EUR": ctx["combined"],
        "Decomposition_residual_EUR": ctx["residual"],
        "Project_import_cost_before_net_billing_EUR": proj["yImportCost"],
        "Project_export_credit_EUR": proj["yCredit"],
        "Project_wallet_burn_EUR": proj["yWalletBurn"],
        "PV_BESS_project_cashflow_EUR": ctx["cash"][1:],
        "Cumulative_discounted_PV_BESS_NPV_EUR": ctx["npv_t"][1:],
    })


def cashflow_timeline_df(s: State, ctx: dict, mode: str = "sc") -> pd.DataFrame:
    """Cashflow timeline. SC uses the corrected MATLAB's PV_BESS_* column names;
    ARB keeps the legacy names of the (not yet patched) MATLAB ARB export."""
    Y = len(s.years)
    r = s.cfg["discount_rate"]
    cash = ctx["cash"]
    year = np.arange(Y + 1)
    df = (1 + r) ** year
    disc = cash / df
    if mode.lower() == "sc":
        names = ("PV_BESS_cashflow_EUR", "Discounted_PV_BESS_cashflow_EUR",
                 "Cumulative_discounted_PV_BESS_NPV_EUR")
    else:
        names = ("Cashflow_EUR", "Discounted_cashflow_EUR",
                 "Cumulative_discounted_cashflow_EUR")
    return pd.DataFrame({
        "Year": year,
        names[0]: cash,
        "Discount_factor": df,
        names[1]: disc,
        names[2]: np.cumsum(disc),
    })


def summary_df(s: State, ctx: dict) -> pd.DataFrame:
    proj = ctx["proj"]
    cap = bess_capacity(s.inp, s.cfg)
    pv_y = proj["yPV"]; charge = proj["yCharge"]; exp = proj["yExport"]
    curt = np.zeros(len(s.years))
    pv_to_load = np.maximum(pv_y - charge - exp - curt, 0.0)
    pv_self_consumed = pv_to_load + charge
    load_tot = proj["yLoad"].sum()
    capex = E.new_project_capex(s.cfg, s.inp.pv_new_kW, cap["new_nominal"])
    wss = 1 - (proj["yImport"].sum() / load_tot if load_tot > 0 else 0.0)
    if load_tot <= 0:
        wss = 0.0
    savings_pe = ctx["savings_same_load"]   # equal-load PV+BESS savings (NPV basis)
    row = {
        "PV_BESS_NPV_EUR": ctx["npv"],
        # Full cashflow incl. the year-0 CAPEX (corrected MATLAB uses sum(cash)).
        "Total_undiscounted_PV_BESS_cashflow_EUR": ctx["cash"].sum(),
        "Average_PV_BESS_savings_same_project_load_EUR_per_year": np.nanmean(savings_pe),
        "Average_EV_load_growth_cost_existing_assets_EUR_per_year": np.nanmean(ctx["ev_growth"]),
        "Average_combined_bill_change_EUR_per_year": np.nanmean(ctx["combined"]),
        "Maximum_decomposition_residual_EUR": float(np.max(np.abs(ctx["residual"]))),
        "Total_project_load_kWh": load_tot,
        "Total_project_PV_generation_kWh": pv_y.sum(),
        "Total_project_grid_import_kWh": proj["yImport"].sum(),
        "Total_project_grid_export_kWh": exp.sum(),
        "Total_project_BESS_charge_kWh": charge.sum(),
        "Total_project_BESS_discharge_kWh": proj["yDischarge"].sum(),
        "Total_project_curtailment_kWh": curt.sum(),
        "Weighted_self_consumption_rate": (pv_self_consumed.sum() / pv_y.sum() if pv_y.sum() else 0.0),
        "Weighted_self_sufficiency_rate": wss,
        "Total_project_import_cost_before_net_billing_EUR": proj["yImportCost"].sum(),
        "Total_project_export_credit_EUR": proj["yCredit"].sum(),
        "Total_project_wallet_burn_EUR": proj["yWalletBurn"].sum(),
        "BESS_degradation_rate": s.inp.bess_degradation_rate,
        "BESS_nominal_kWh_year_end": cap["nominal"][-1],
        "BESS_effective_kWh_year_end": cap["eff_total"][-1],
        "CAPEX_PV_new_EUR": capex["PV_new_EUR"],
        "CAPEX_BESS_new_EUR": capex["BESS_new_EUR"],
        "CAPEX_total_new_EUR": capex["Total_new_EUR"],
    }
    row.update(screening_summary(grid_screening_df(s, proj, s.cfg), s.inp.area_type))
    return pd.DataFrame([row])


def sc_flows_df(s: State, sim: dict) -> pd.DataFrame:
    pv_y = sim["yPV"]; charge = sim["yCharge"]; exp = sim["yExport"]
    curt = np.zeros(len(s.years))
    pv_to_load = np.maximum(pv_y - charge - exp - curt, 0.0)
    return pd.DataFrame({
        "Year": s.years,
        "PV_to_load_kWh": pv_to_load,
        "PV_to_BESS_kWh": charge,
        "PV_to_grid_kWh": exp,
        "PV_curtailed_kWh": curt,
        "Grid_to_load_kWh": sim["yImport"],
        "BESS_to_load_kWh": sim["yDischarge"],
    })


def load_profiles_df(s: State) -> pd.DataFrame:
    consumers_y = s.load_consumers.sum(axis=1)
    ev_baseline_y = s.ev_baseline_y.sum(axis=1)
    ev_project_y = s.ev_project_y.sum(axis=1)
    existing_y = s.load_existing.sum(axis=1)
    project_y = s.load_project.sum(axis=1)
    return pd.DataFrame({
        "Year": s.years,
        "Consumers_load_without_EV_kWh": consumers_y,
        "EV_baseline_load_kWh": ev_baseline_y,
        "EV_project_load_kWh": ev_project_y,
        "Consumers_plus_EV_baseline_kWh": existing_y,
        "Consumers_plus_EV_project_kWh": project_y,
    })


def price_annual_df(s: State) -> pd.DataFrame:
    return pd.DataFrame({
        "Year": s.years,
        "Buy_price_avg_EUR_kWh": s.buy.mean(axis=1),
        "Buy_price_min_EUR_kWh": s.buy.min(axis=1),
        "Buy_price_max_EUR_kWh": s.buy.max(axis=1),
        "Sell_price_avg_EUR_kWh": s.sell.mean(axis=1),
        "Sell_price_min_EUR_kWh": s.sell.min(axis=1),
        "Sell_price_max_EUR_kWh": s.sell.max(axis=1),
    })


def bess_degradation_df(inp: Inputs, cfg: dict) -> pd.DataFrame:
    c = bess_capacity(inp, cfg)
    loss = c["nominal"] - c["eff_total"]
    with np.errstate(divide="ignore", invalid="ignore"):
        loss_rate = np.where(c["nominal"] > 0, loss / c["nominal"], 0.0)
    return pd.DataFrame({
        "Year": inp.years,
        "BESS_adoption_factor": c["adoption"],
        "BESS_nominal_kWh": c["nominal"],
        "BESS_existing_effective_kWh": c["eff_existing"],
        "BESS_new_effective_kWh": c["eff_new"],
        "BESS_total_effective_kWh": c["eff_total"],
        "BESS_power_kW": c["power"],
        "BESS_degradation_loss_kWh": loss,
        "BESS_degradation_loss_rate": loss_rate,
    })


def price_timeseries_df(s: State) -> pd.DataFrame:
    Y = len(s.years)
    return pd.DataFrame({
        "Year": np.repeat(s.years, N),
        "Step_15min": np.tile(np.arange(1, N + 1), Y),
        "Billing_day": np.tile(s.idx_day, Y),
        "Period_15min": np.tile(s.idx_period, Y),
        "Buy_price_EUR_kWh": s.buy.reshape(-1),   # row-major = year-major (matches buy.'(:) )
        "Sell_price_EUR_kWh": s.sell.reshape(-1),
    })


# =====================================================================
# PCC voltage screening — mode-agnostic
# =====================================================================

def grid_screening_df(s: State, sim: dict, cfg: dict) -> pd.DataFrame:
    """Per-milestone-year PCC voltage screening of the project scenario.

    Uses the per-year peak import/export power tracked by the dispatch. Only the
    configured milestone years (plus the final year) are screened; 3-phase 400 V.
    The impedance preset comes from Input!C4 Area Type.
    """
    years = [int(y) for y in s.years]
    R, X = GS.impedance(s.inp.area_type)
    pf = cfg["power_factor"]
    v_grid = cfg["V_grid_pu"]
    peak_imp = sim["yPeakImport"]
    peak_exp = sim["yPeakExport"]

    sel = GS.milestone_years(years, cfg.get("screening_years", []))
    idx_of = {y: i for i, y in enumerate(years)}

    rows = []
    for y in sel:
        i = idx_of[y]
        imp = GS.screen_point(float(peak_imp[i]), R, X, pf, v_grid, is_export=False)
        exp = GS.screen_point(float(peak_exp[i]), R, X, pf, v_grid, is_export=True)
        rows.append({
            "Year": y,
            "Peak_import_kW": float(peak_imp[i]),
            "Peak_export_kW": float(peak_exp[i]),
            "Peak_import_kVA": imp["S_kVA"],
            "Peak_export_kVA": exp["S_kVA"],
            "I_peak_A": max(imp["I_A"], exp["I_A"]),
            "dV_import_pu": imp["dV_pu"],
            "dV_export_pu": exp["dV_pu"],
            "V_PCC_min_pu": imp["V_PCC_pu"],
            "V_PCC_max_pu": exp["V_PCC_pu"],
        })
    return pd.DataFrame(rows)


def screening_summary(gdf: pd.DataFrame, area_type: str) -> dict:
    """Aggregate the per-year screening into headline Summary fields."""
    if gdf.empty:
        return {}
    return {
        "Grid_connection_type": area_type,
        "Grid_peak_import_kW": float(gdf["Peak_import_kW"].max()),
        "Grid_peak_export_kW": float(gdf["Peak_export_kW"].max()),
        "Grid_V_PCC_min_pu": float(gdf["V_PCC_min_pu"].min()),
        "Grid_V_PCC_max_pu": float(gdf["V_PCC_max_pu"].max()),
    }


# =====================================================================
# ARB mode (flow-based rolling-horizon LP dispatch)
# =====================================================================

def project_sim_arb(s: State) -> dict:
    """Project scenario: existing+new PV/BESS, project load, rolling ARB LP."""
    cap = bess_capacity(s.inp, s.cfg)
    return A.simulate_arb(
        s.load_project, s.pv_project, cap["eff_total"], cap["power"],
        s.cfg["DoD"], s.cfg["eta_ch"], s.cfg["eta_dis"],
        s.buy, s.sell, s.idx_month, s.cfg,
        s.sell_ordinary if s.green_origin_tracking else None,
        cap["replacement_years"],
    )


def existing_sim_arb(s: State) -> dict:
    """Existing assets (existing PV + existing degraded BESS) at the BASELINE load."""
    cap = bess_capacity(s.inp, s.cfg)
    return A.simulate_arb(
        s.load_existing, s.pv_existing, cap["eff_existing"], cap["power_existing"],
        s.cfg["DoD"], s.cfg["eta_ch"], s.cfg["eta_dis"],
        s.buy, s.sell, s.idx_month, s.cfg,
        s.sell_ordinary if s.green_origin_tracking else None,
    )


def existing_sim_arb_project_load(s: State) -> dict:
    """Existing assets at the PROJECT load — counterfactual baseline for ARB NPV."""
    cap = bess_capacity(s.inp, s.cfg)
    return A.simulate_arb(
        s.load_project, s.pv_existing, cap["eff_existing"], cap["power_existing"],
        s.cfg["DoD"], s.cfg["eta_ch"], s.cfg["eta_dis"],
        s.buy, s.sell, s.idx_month, s.cfg,
        s.sell_ordinary if s.green_origin_tracking else None,
    )


def run_scenarios_arb(s: State) -> dict:
    """Run the ARB scenarios + equal-load cashflow + NPV.

    Same equal-load decomposition as SC: NPV uses PV+BESS savings measured at the
    project load, so EV-load growth no longer contaminates it. NOTE: the MATLAB
    ARB reference has not yet received the SC decomposition patch, so ARB parity
    against the current golden will diverge until MATLAB ARB is patched too.
    """
    # Run dispatches sequentially. Concurrent HiGHS solves from two threads froze
    # the web server (thread-start deadlock, scipy 1.18 / Python 3.13 Windows);
    # sequential Stage-2 measured the same ~56 s on the reference workload.
    proj = project_sim_arb(s)
    exist = existing_sim_arb(s)                      # existing assets @ baseline load
    exist_pl = existing_sim_arb_project_load(s)
    grid = A.grid_only_bill_y(s.buy, s.load_grid)
    grid_pl = A.grid_only_bill_y(s.buy, s.load_project)
    Y = len(s.years)
    fPV = adoption_curve(Y, s.inp.pv_adoption, Y)
    fBESS = adoption_curve(Y, s.cfg["BESS_adoption"], Y)
    dec = decompose_savings(exist["yBill"], exist_pl["yBill"], proj["yBill"])
    cap = bess_capacity(s.inp, s.cfg)
    cash = E.build_cashflow(s.cfg, s.inp.pv_new_kW, cap["new_nominal"],
                            dec["savings_same_load"], fPV, fBESS)
    npv, npv_t = E.compute_npv(cash, s.cfg["discount_rate"])
    return {"proj": proj, "exist": exist, "exist_pl": exist_pl,
            "grid": grid, "grid_pl": grid_pl, **dec,
            "cash": cash, "npv": npv, "npv_t": npv_t}


def energy_balance_arb_df(s: State, sim: dict) -> pd.DataFrame:
    pv_existing_y = s.pv_existing.sum(axis=1) * DT_H
    pv_new_y = s.pv_new.sum(axis=1) * DT_H
    pv_y = sim["yPV"]
    imp = sim["yImport"]; exp = sim["yExport"]
    curt = sim["yCurtail"]; charge = sim["yCharge"]; discharge = sim["yDischarge"]
    load_y = sim["yLoad"]
    pv_utilised = sim["yearPV_to_load"] + sim["yearPV_to_BESS"]
    self_supply = 1 - _safe_div(sim["yearGrid_to_load"], load_y)
    self_supply = np.where(load_y <= 0, 0.0, self_supply)
    return pd.DataFrame({
        "Year": s.years,
        "Grid_only_load_kWh": s.load_grid.sum(axis=1),
        "Existing_load_kWh": s.load_existing.sum(axis=1),
        "Project_load_kWh": load_y,
        "PV_existing_generation_kWh": pv_existing_y,
        "PV_new_generation_kWh": pv_new_y,
        "PV_project_generation_kWh": pv_y,
        "Project_total_grid_import_kWh": imp,
        "Project_total_grid_export_kWh": exp,
        "Project_curtailment_kWh": curt,
        "Project_BESS_charge_kWh": charge,
        "Project_BESS_discharge_kWh": discharge,
        "PV_utilisation_rate_before_origin_tracking": _safe_div(pv_utilised, pv_y),
        "Load_self_supply_rate": self_supply,
        "Export_rate_vs_PV_generation": _safe_div(exp, pv_y),
        "Curtailment_rate_vs_PV_generation": _safe_div(curt, pv_y),
    })


def arb_flows_df(s: State, sim: dict) -> pd.DataFrame:
    return pd.DataFrame({
        "Year": s.years,
        "PV_to_load_kWh": sim["yearPV_to_load"],
        "PV_to_BESS_kWh": sim["yearPV_to_BESS"],
        "PV_to_grid_kWh": sim["yearPV_to_grid"],
        "PV_curtailed_kWh": sim["yearPV_curtailed"],
        "Grid_to_load_kWh": sim["yearGrid_to_load"],
        "Grid_to_BESS_kWh": sim["yearGrid_to_BESS"],
        "BESS_to_load_kWh": sim["yearBESS_to_load"],
        "BESS_to_grid_kWh": sim["yearBESS_to_grid"],
        "BESS_PV_to_load_kWh": sim["yearBESS_PV_to_load"],
        "BESS_PV_to_grid_kWh": sim["yearBESS_PV_to_grid"],
        "BESS_grid_to_load_kWh": sim["yearBESS_grid_to_load"],
        "BESS_grid_to_grid_kWh": sim["yearBESS_grid_to_grid"],
    })


def economics_arb_df(s: State, ctx: dict) -> pd.DataFrame:
    grid = ctx["grid"]; grid_pl = ctx["grid_pl"]
    exist = ctx["exist"]; exist_pl = ctx["exist_pl"]; proj = ctx["proj"]
    return pd.DataFrame({
        "Year": s.years,
        "Grid_only_bill_EUR": grid,
        "Existing_ARB_bill_EUR": exist["yBill"],
        "Project_ARB_bill_EUR": proj["yBill"],
        "Savings_existing_ARB_vs_grid_EUR": grid - exist["yBill"],
        "Savings_project_ARB_vs_grid_EUR": grid_pl - proj["yBill"],
        "Savings_project_ARB_vs_existing_ARB_EUR": ctx["savings_same_load"],
        # Causal decomposition (naming follows the corrected MATLAB SC export, so it
        # lines up once the MATLAB ARB model receives the same patch).
        "Existing_assets_bill_project_load_EUR": exist_pl["yBill"],
        "PV_BESS_savings_same_project_load_EUR": ctx["savings_same_load"],
        "EV_load_growth_cost_existing_assets_EUR": ctx["ev_growth"],
        "Combined_bill_change_original_to_project_EUR": ctx["combined"],
        "Decomposition_residual_EUR": ctx["residual"],
        "Grid_to_BESS_cost_EUR": proj["yGridToBESSCost"],
        "BESS_to_grid_revenue_EUR": proj["yBESSToGridRev"],
        "BESS_PV_to_grid_revenue_EUR": proj["yBESSPVToGridRev"],
        "BESS_grid_to_grid_revenue_EUR": proj["yBESSGridToGridRev"],
        "PV_to_grid_revenue_EUR": proj["yPVToGridRev"],
        "Project_cashflow_EUR": ctx["cash"][1:],
        "Cumulative_discounted_NPV_EUR": ctx["npv_t"][1:],
    })


def summary_arb_df(s: State, ctx: dict) -> pd.DataFrame:
    proj = ctx["proj"]
    cap = bess_capacity(s.inp, s.cfg)
    pv_y = proj["yPV"]
    pv_utilised = proj["yearPV_to_load"] + proj["yearPV_to_BESS"]
    load_tot = proj["yLoad"].sum()
    capex = E.new_project_capex(s.cfg, s.inp.pv_new_kW, cap["new_nominal"])
    wlss = 1 - (proj["yearGrid_to_load"].sum() / load_tot if load_tot > 0 else 0.0)
    if load_tot <= 0:
        wlss = 0.0
    savings_pe = ctx["savings_same_load"]   # equal-load PV+BESS savings (NPV basis)
    mcpd = proj["max_cycles_per_day"]
    row = {
        "NPV_EUR": ctx["npv"],
        "Total_undiscounted_cashflow_EUR": ctx["cash"][1:].sum(),
        "Average_annual_savings_Project_ARB_vs_Existing_ARB_EUR": np.nanmean(savings_pe),
        "Average_EV_load_growth_cost_existing_assets_EUR_per_year": np.nanmean(ctx["ev_growth"]),
        "Average_combined_bill_change_EUR_per_year": np.nanmean(ctx["combined"]),
        "Maximum_decomposition_residual_EUR": float(np.max(np.abs(ctx["residual"]))),
        "Total_project_load_20y_kWh": load_tot,
        "Total_project_PV_generation_20y_kWh": pv_y.sum(),
        "Total_project_grid_import_20y_kWh": proj["yImport"].sum(),
        "Total_project_grid_export_20y_kWh": proj["yExport"].sum(),
        "Total_Grid_to_BESS_20y_kWh": proj["yearGrid_to_BESS"].sum(),
        "Total_BESS_to_grid_20y_kWh": proj["yearBESS_to_grid"].sum(),
        "Total_BESS_to_load_20y_kWh": proj["yearBESS_to_load"].sum(),
        "Total_project_curtailment_20y_kWh": proj["yCurtail"].sum(),
        "Weighted_PV_utilisation_rate_before_origin_tracking":
            (pv_utilised.sum() / pv_y.sum() if pv_y.sum() else 0.0),
        "Weighted_load_self_supply_rate": wlss,
        "Total_Grid_to_BESS_cost_EUR": proj["yGridToBESSCost"].sum(),
        "Total_BESS_to_grid_revenue_EUR": proj["yBESSToGridRev"].sum(),
        "Total_PV_to_grid_revenue_EUR": proj["yPVToGridRev"].sum(),
        "BESS_degradation_rate": s.inp.bess_degradation_rate,
        "Max_cycles_per_day": mcpd if np.isfinite(mcpd) else np.nan,
        "BESS_nominal_kWh_year_end": cap["nominal"][-1],
        "BESS_effective_kWh_year_end": cap["eff_total"][-1],
        "CAPEX_PV_new_EUR": capex["PV_new_EUR"],
        "CAPEX_BESS_new_EUR": capex["BESS_new_EUR"],
        "CAPEX_total_new_EUR": capex["Total_new_EUR"],
    }
    row.update(screening_summary(grid_screening_df(s, proj, s.cfg), s.inp.area_type))
    return pd.DataFrame([row])
