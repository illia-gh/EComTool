"""Plot key charts from an ARB or SC Results workbook.

Reads Excel output produced by either MATLAB model and renders a 6-panel
matplotlib figure plus a KPI banner. Kept dependency-light (pandas +
matplotlib) so it survives the eventual MATLAB->Python port unchanged.

Usage:
    python tool/scripts/plot_results.py [RESULTS_XLSX] [-o OUT_PNG] [--show]
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Mapping

import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.ticker import FuncFormatter


def _fmt_thousands(x, _pos=None) -> str:
    """Plain integer with thin-space thousands separator (no 1e6 / scientific)."""
    return f"{x:,.0f}".replace(",", " ")


_THOUSANDS = FuncFormatter(_fmt_thousands)


def _axis_style(ax, ylabel: str) -> None:
    """Consistent axis: plain thousands-separated y ticks, grid, integer year x."""
    ax.yaxis.set_major_formatter(_THOUSANDS)   # plain thousands, no scientific offset
    ax.set_xlabel("Year")
    ax.set_ylabel(ylabel)
    ax.grid(alpha=0.3)


def _legend(ax, *, fontsize=None):
    """Use one semi-transparent, upper-right legend style across every panel."""
    kwargs = {
        "loc": "upper right",
        "frameon": True,
        "framealpha": 0.7,
        "facecolor": "white",
        "edgecolor": "grey",
    }
    if fontsize is not None:
        kwargs["fontsize"] = fontsize
    legend = ax.legend(**kwargs)
    try:
        panel = next(
            index for index, item in enumerate(ax.figure.axes, 1)
            if item is ax
        )
    except (StopIteration, TypeError):
        panel = 1
    legend.set_gid(f"chart-legend-{panel}")
    return legend


def _annotate_payback(ax, payback: int):
    """Mark discounted payback with stable wording and left alignment."""
    ax.axvline(payback, color=_C_EXISTING, lw=0.8, ls=":")
    return ax.annotate(
        f"payback ≈ {payback} years",
        (payback, 0),
        textcoords="offset points",
        xytext=(6, 8),
        color=_C_EXISTING,
        fontsize=9,
        ha="left",
    )

REPO = Path(__file__).resolve().parents[2]
DEFAULT_XLSX = (
    REPO / "RawData" / "EComTool" / "MATLAB Part" / "ARB"
    / "Results_PV_BESS_ARB_20y.xlsx"
)
DEFAULT_OUT = REPO / "outputs" / "arb_report.png"


def load(xlsx: Path, sheet_names: tuple[str, ...] | list[str] | None = None) -> dict[str, pd.DataFrame]:
    with pd.ExcelFile(xlsx, engine="openpyxl") as xf:
        selected = xf.sheet_names if sheet_names is None else [s for s in sheet_names if s in xf.sheet_names]
        return {s: pd.read_excel(xf, sheet_name=s) for s in selected}


def result_mode(sheets: Mapping[str, pd.DataFrame]) -> str:
    """Return the Results workbook mode from its mode-specific flow sheet."""
    if "ARB_Flows" in sheets:
        return "arb"
    if "SC_Flows" in sheets:
        return "sc"
    raise ValueError("Results workbook must contain ARB_Flows or SC_Flows")


def eur(v: float) -> str:
    return f"{v:,.0f} EUR".replace(",", " ")


# Logical name -> candidate column names, newest first. The corrected SC export
# (export_pv_bess_sc_results.m, EV-load decomposition patch) renamed most economic
# columns; the ARB export has not been patched and keeps the legacy names. Listing
# candidates keeps one code path working against both schemas.
_COLUMN_CANDIDATES = {
    "npv":                 ["PV_BESS_NPV_EUR", "NPV_EUR"],
    "avg_savings":         ["Average_PV_BESS_savings_same_project_load_EUR_per_year",
                            "Average_annual_savings_Project_{tag}_vs_Existing_{tag}_EUR"],
    "cum_discounted":      ["Cumulative_discounted_PV_BESS_NPV_EUR",
                            "Cumulative_discounted_cashflow_EUR"],
    "grid_bill":           ["Grid_bill_baseline_load_EUR", "Grid_only_bill_EUR"],
    "grid_bill_project":   ["Grid_bill_project_load_EUR"],
    "existing_bill":       ["Existing_assets_bill_baseline_load_EUR", "Existing_{tag}_bill_EUR"],
    "existing_bill_project": ["Existing_assets_bill_project_load_EUR"],
    "project_bill":        ["Project_assets_bill_project_load_EUR", "Project_{tag}_bill_EUR"],
    "existing_savings":    ["Existing_assets_savings_vs_grid_baseline_load_EUR",
                            "Savings_existing_{tag}_vs_grid_EUR"],
    "project_savings":     ["Project_assets_savings_vs_grid_project_load_EUR",
                            "Savings_project_{tag}_vs_grid_EUR"],
    "incremental_savings": ["PV_BESS_savings_same_project_load_EUR",
                            "Savings_project_{tag}_vs_existing_{tag}_EUR"],
    "total_pv":            ["Total_project_PV_generation_kWh",
                            "Total_project_PV_generation_20y_kWh"],
    "total_import":        ["Total_project_grid_import_kWh",
                            "Total_project_grid_import_20y_kWh"],
    "total_export":        ["Total_project_grid_export_kWh",
                            "Total_project_grid_export_20y_kWh"],
}


def column(available, key: str, tag: str) -> str:
    """Resolve a logical column name against the columns actually present."""
    names = set(available)
    for candidate in _COLUMN_CANDIDATES[key]:
        name = candidate.format(tag=tag)
        if name in names:
            return name
    wanted = [c.format(tag=tag) for c in _COLUMN_CANDIDATES[key]]
    raise KeyError(f"none of {wanted} found; available: {sorted(names)[:12]}...")


def column_or_none(available, key: str, tag: str) -> str | None:
    """Like `column`, but returns None instead of raising.

    Used for series that only exist in the corrected SC schema, so a legacy ARB
    workbook simply plots fewer lines instead of failing.
    """
    try:
        return column(available, key, tag)
    except KeyError:
        return None


# Consistent series colors so the same source reads the same across every chart.
_C_GRID = "#d62728"; _C_PV = "#ff7f0e"; _C_BESS = "#9467bd"
_C_PROJECT = "#1f77b4"; _C_EXISTING = "#2ca02c"


def build(sheets: Mapping[str, pd.DataFrame], xlsx: Path):
    mode = result_mode(sheets)
    tag = mode.upper()
    mode_label = "Arbitrage" if mode == "arb" else "Self-consumption"
    summ = sheets["Summary"].iloc[0]
    econ = sheets["Economics"]
    flows = sheets[f"{tag}_Flows"]
    ebal = sheets["Energy_Balance"]
    cash = sheets["Cashflow_Timeline"]
    bess = sheets["BESS_Degradation"]
    lp = sheets["Load_Profiles"]

    avg_savings_col = column(summ.index, "avg_savings", tag)
    npv_col = column(summ.index, "npv", tag)
    grid_bill_col = column(econ.columns, "grid_bill", tag)
    # The project-load bills are the equal-load counterfactual: mandatory in the
    # corrected SC schema (a workbook missing them is stale or damaged, and must not
    # be charted silently), absent by design in the legacy ARB export.
    if mode == "sc":
        grid_bill_pl_col = column(econ.columns, "grid_bill_project", tag)
        existing_bill_pl_col = column(econ.columns, "existing_bill_project", tag)
    else:
        grid_bill_pl_col = column_or_none(econ.columns, "grid_bill_project", tag)
        existing_bill_pl_col = column_or_none(econ.columns, "existing_bill_project", tag)
    existing_bill_col = column(econ.columns, "existing_bill", tag)
    project_bill_col = column(econ.columns, "project_bill", tag)
    existing_savings_col = column(econ.columns, "existing_savings", tag)
    project_savings_col = column(econ.columns, "project_savings", tag)
    incremental_savings_col = column(econ.columns, "incremental_savings", tag)
    cum_disc_col = column(cash.columns, "cum_discounted", tag)

    # Panel 7 uses mode-specific efficiency rates (SC vs ARB Energy_Balance columns).
    if mode == "arb":
        rate_series = [("PV_utilisation_rate_before_origin_tracking", "PV utilisation", _C_PV),
                       ("Load_self_supply_rate", "Load self-supply", _C_PROJECT)]
        rate_title = "7. PV utilisation & load self-supply"
    else:
        rate_series = [("Project_self_consumption_rate", "PV self-consumption", _C_PV),
                       ("Project_self_sufficiency_rate", "Load self-sufficiency", _C_PROJECT)]
        rate_title = "7. Self-consumption & self-sufficiency"

    # 8 panels in MATLAB figure order (export_pv_bess_{sc,arb}_results.m); all energy
    # in kWh, all axes plain thousands-separated (no MWh, no 1e6 offset).
    fig, axes = plt.subplots(4, 2, figsize=(14, 20))
    fig.subplots_adjust(top=0.93, hspace=0.34, wspace=0.22)
    ax = axes.ravel()

    npv = summ[npv_col]
    horizon = int(cash["Year"].max())
    payback = next((int(y) for y, c in zip(cash["Year"], cash[cum_disc_col]) if c >= 0), None)
    payback_text = str(payback) if payback is not None else f">{horizon}"
    banner = (
        f"PV+BESS {mode_label} — {horizon}-year results\n"
        f"NPV = {eur(npv)}    |    Avg savings = "
        f"{eur(summ[avg_savings_col])}/yr    |    "
        f"CAPEX = {eur(summ['CAPEX_total_new_EUR'])}    |    "
        f"Discounted payback = {payback_text} yr"
    )
    fig.suptitle(banner, fontsize=15, fontweight="bold", ha="center")

    yrs = econ["Year"]

    # 1) Annual load profiles — the two loads the scenarios are actually run at, so
    #    the EV-load increase is visible as the gap between them.
    a = ax[0]
    a.plot(lp["Year"], lp["Consumers_plus_EV_baseline_kWh"], "-o", color=_C_PROJECT,
           label="Baseline load (consumers + existing EVs)")
    a.plot(lp["Year"], lp["Consumers_plus_EV_project_kWh"], "-o", color=_C_PV,
           label="Project load (consumers + project EVs)")
    a.set_title("1. Annual load profiles"); _axis_style(a, "Annual load (kWh)"); _legend(a, fontsize=8)

    # 2) PV generation by year
    a = ax[1]
    a.plot(ebal["Year"], ebal["PV_project_generation_kWh"], "-o", color=_C_PV)
    a.set_title("2. PV generation by year"); _axis_style(a, "PV generation (kWh)")

    # 3) Annual electricity bill. Every series is labelled with the load it was
    #    computed at: the PV+BESS effect is the gap between the two *project-load*
    #    bills, never the gap between a baseline-load and a project-load bill.
    a = ax[2]
    a.plot(yrs, econ[grid_bill_col], "-o", color=_C_GRID,
           label="Grid only, baseline load")
    if grid_bill_pl_col:
        a.plot(yrs, econ[grid_bill_pl_col], "--o", color=_C_GRID, alpha=0.6,
               label="Grid only, project load")
    a.plot(yrs, econ[existing_bill_col], "-o", color=_C_EXISTING,
           label="Existing assets, baseline load")
    if existing_bill_pl_col:
        a.plot(yrs, econ[existing_bill_pl_col], "--o", color=_C_EXISTING, alpha=0.6,
               label="Existing assets, project load")
    a.plot(yrs, econ[project_bill_col], "-o", color=_C_PROJECT,
           label="Project assets, project load")
    a.set_title("3. Annual electricity bill"); _axis_style(a, "Electricity bill (EUR)")
    _legend(a, fontsize=8)

    # 4) Annual savings — the incremental series is the equal-load PV+BESS effect.
    a = ax[3]
    a.plot(yrs, econ[existing_savings_col], "-o", label="Existing vs grid (baseline load)")
    a.plot(yrs, econ[project_savings_col], "-o", label="Project vs grid (project load)")
    a.plot(yrs, econ[incremental_savings_col], "-o",
           label="PV+BESS effect (both at project load)")
    a.set_title("4. Annual savings"); _axis_style(a, "Savings (EUR)"); _legend(a, fontsize=8)

    # 5) NPV trajectory (cumulative discounted cashflow / payback curve)
    a = ax[4]
    a.plot(cash["Year"], cash[cum_disc_col], "-o", color=_C_PROJECT)
    a.axhline(0, color="grey", lw=0.8, ls="--")
    if payback is not None:
        _annotate_payback(a, payback)
    a.set_title("5. NPV trajectory (cumulative discounted)"); _axis_style(a, "Cumulative NPV (EUR)")

    # 6) BESS nominal vs effective capacity (degradation)
    a = ax[5]
    a.plot(bess["Year"], bess["BESS_nominal_kWh"], "-o", color="#8c564b",
           label="Nominal installed")
    a.plot(bess["Year"], bess["BESS_total_effective_kWh"], "-o", color=_C_BESS,
           label="Effective after degradation")
    a.set_title("6. BESS nominal vs effective capacity"); _axis_style(a, "BESS capacity (kWh)"); _legend(a, fontsize=8)

    # 7) Self-consumption / self-sufficiency rates
    a = ax[6]
    for col, lab, colr in rate_series:
        a.plot(ebal["Year"], ebal[col] * 100, "-o", color=colr, label=lab)
    a.set_title(rate_title); _axis_style(a, "Rate (%)"); _legend(a, fontsize=8)

    # 8) Load coverage by source — where each year's load energy comes from (kWh)
    a = ax[7]
    f = flows.set_index("Year")
    parts = [
        ("PV_to_load_kWh", "PV → load", _C_PV),
        ("BESS_to_load_kWh", "BESS → load", _C_BESS),
        ("Grid_to_load_kWh", "Grid → load", _C_GRID),
    ]
    b = 0
    for col, lab, colr in parts:
        a.bar(f.index, f[col], bottom=b, label=lab, color=colr)
        b = b + f[col]
    a.set_title("8. Load coverage by source"); _axis_style(a, "Energy to load (kWh)"); _legend(a, fontsize=8)

    fig.text(0.5, 0.005, f"source: {xlsx.name}", ha="center", fontsize=8, color="grey")
    return fig


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("xlsx", nargs="?", default=str(DEFAULT_XLSX), help="Results xlsx path")
    p.add_argument("-o", "--out", default=str(DEFAULT_OUT), help="output PNG path")
    p.add_argument("--show", action="store_true", help="open interactive window")
    args = p.parse_args()

    if not args.show:
        matplotlib.use("Agg")

    xlsx = Path(args.xlsx)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    sheets = load(xlsx)
    fig = build(sheets, xlsx)
    fig.savefig(out, dpi=130, bbox_inches="tight")
    print(f"wrote {out}")
    if args.show:
        plt.show()


if __name__ == "__main__":
    main()
