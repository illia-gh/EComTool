"""Render a self-contained HTML report from ARB or SC result tables.

Reads the Results schema (the 9 sheets emitted by the MATLAB ARB model, and
later by the Python compute port) and writes a single standalone .html file:
KPI tiles + embedded charts (inline SVG) + key tables. No external assets, so
the file can be opened offline or emailed as-is.

The report depends only on the Results *schema*, not on how it was computed --
so it is unchanged when the MATLAB compute core is replaced by the Python port.

Usage:
    python tool/scripts/report.py [RESULTS_XLSX] [-o OUT_HTML]
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import math
from pathlib import Path
from typing import Mapping

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from plot_results import DEFAULT_XLSX, build, column, load as load_results, result_mode

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "outputs" / "report.html"
REPORT_SHEETS = (
    "Summary", "Energy_Balance", "Load_Profiles", "ARB_Flows", "SC_Flows",
    "Economics", "BESS_Degradation", "Cashflow_Timeline", "Grid_Screening",
)
VOLTAGE_MIN_PU = 0.80
VOLTAGE_MAX_PU = 1.20
VOLTAGE_RANGE_ERROR = "Voltage beyond the range"


def load(xlsx: Path) -> dict[str, pd.DataFrame]:
    """Read report-only sheets; skip large Price_TimeSeries workbook payload."""
    return load_results(xlsx, sheet_names=REPORT_SHEETS)


def fig_to_svg(fig) -> str:
    buf = io.StringIO()
    fig.savefig(buf, format="svg", bbox_inches="tight")
    svg = buf.getvalue()
    plt.close(fig)
    # strip XML/doctype preamble so it embeds cleanly inline
    return svg[svg.index("<svg"):]


def eur(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") + " €"


def kwh(v: float, unit="kWh") -> str:
    return f"{v:,.0f}".replace(",", " ") + f" {unit}"


def pct(v: float) -> str:
    return f"{v * 100:.1f} %"


def tile(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="tile-sub">{html.escape(sub)}</div>' if sub else ""
    return (
        f'<div class="tile"><div class="tile-val">{html.escape(value)}</div>'
        f'<div class="tile-label">{html.escape(label)}</div>{sub_html}</div>'
    )


def table(df: pd.DataFrame, round_to: int = 1, column_labels: Mapping[str, str] | None = None) -> str:
    """Render a DataFrame as an HTML table with optional display-only labels."""
    d = df.copy()

    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].round(round_to)

    result = d.to_html(index=False, border=0, classes="data", justify="left")

    if column_labels:
        for column_name, display_name in column_labels.items():
            escaped_column = html.escape(str(column_name))
            result = result.replace(
                f"<th>{escaped_column}</th>",
                f"<th>{display_name}</th>",
            )

    return result


def _voltage_number(raw) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float("nan")


def _voltage_display(raw) -> str:
    value = _voltage_number(raw)
    if math.isfinite(value) and VOLTAGE_MIN_PU <= value <= VOLTAGE_MAX_PU:
        return f"{value:.3f}"
    return VOLTAGE_RANGE_ERROR


def screening_section(gdf: pd.DataFrame, summ: pd.Series) -> str:
    """Render peak grid-loading metrics and validated PCC voltage bounds."""

    report_columns = (
        "Year",
        "Peak_import_kW",
        "Peak_export_kW",
        "Peak_import_kVA",
        "Peak_export_kVA",
        "I_peak_A",
        "V_PCC_min_pu",
        "V_PCC_max_pu",
    )

    shown = gdf[[column for column in report_columns if column in gdf.columns]].copy()

    for column_name in ("V_PCC_min_pu", "V_PCC_max_pu"):
        if column_name in shown:
            shown[column_name] = shown[column_name].map(_voltage_display)

    t = table(
        shown,
        round_to=3,
        column_labels={
            "Peak_import_kW": "Peak import (kW)",
            "Peak_export_kW": "Peak export (kW)",
            "Peak_import_kVA": "Peak import (kVA)",
            "Peak_export_kVA": "Peak export (kVA)",
            "I_peak_A": "I<sub>peak</sub> (A)",
            "V_PCC_min_pu": "V<sub>PCC</sub><sup>min</sup> (p.u.)",
            "V_PCC_max_pu": "V<sub>PCC</sub><sup>max</sup> (p.u.)",
        },
    )

    note = (
        '<p class="note">Accepted voltage display range: 0.80–1.20 p.u.</p>'
    )

    return (
        f'<section><h2>Grid screening at PCC</h2>'
        f'{note}<div class="tablewrap">{t}</div></section>\n'
    )


ALL_SECTIONS = ("charts", "economics", "energy")

DRAGGABLE_LEGENDS_SCRIPT = r"""<script>
(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const STORAGE_PREFIX = "ecomtool:chart-legend:";

  document.querySelectorAll('.chart svg g[id^="chart-legend-"]').forEach(legend => {
    const svg = legend.ownerSVGElement;
    const axes = legend.closest('g[id^="axes_"]');
    const plotPatch = axes && Array.from(axes.children)
      .find(child => child.id && child.id.startsWith("patch_"));
    const coordinateRoot = axes || svg;
    const bounds = (plotPatch || coordinateRoot).getBBox();
    const legendBox = legend.getBBox();
    const storageKey = `${STORAGE_PREFIX}${location.pathname}:${legend.id}`;

    const wrapper = document.createElementNS(SVG_NS, "g");
    legend.parentNode.insertBefore(wrapper, legend);
    wrapper.appendChild(legend);

    const hitbox = document.createElementNS(SVG_NS, "rect");
    hitbox.setAttribute("x", legendBox.x);
    hitbox.setAttribute("y", legendBox.y);
    hitbox.setAttribute("width", legendBox.width);
    hitbox.setAttribute("height", legendBox.height);
    hitbox.setAttribute("fill", "transparent");
    hitbox.setAttribute("pointer-events", "all");
    legend.insertBefore(hitbox, legend.firstChild);

    legend.classList.add("draggable-legend");
    legend.setAttribute("tabindex", "0");
    legend.setAttribute("role", "group");
    legend.setAttribute(
      "aria-label",
      "Chart legend. Drag to move; double-click or press Escape to reset."
    );

    const limits = {
      minX: bounds.x - legendBox.x,
      maxX: bounds.x + bounds.width - legendBox.x - legendBox.width,
      minY: bounds.y - legendBox.y,
      maxY: bounds.y + bounds.height - legendBox.y - legendBox.height
    };
    const clampAxis = (value, min, max) => max < min ? 0 : Math.min(max, Math.max(min, value));
    const clamp = value => ({
      x: clampAxis(value.x, limits.minX, limits.maxX),
      y: clampAxis(value.y, limits.minY, limits.maxY)
    });
    const readOffset = () => {
      try {
        const value = JSON.parse(localStorage.getItem(storageKey));
        return value && Number.isFinite(value.x) && Number.isFinite(value.y)
          ? value
          : {x: 0, y: 0};
      } catch (_error) {
        return {x: 0, y: 0};
      }
    };
    const saveOffset = value => {
      try {
        localStorage.setItem(storageKey, JSON.stringify(value));
      } catch (_error) {
        // Dragging still works when storage is unavailable (for example file://).
      }
    };
    const removeOffset = () => {
      try {
        localStorage.removeItem(storageKey);
      } catch (_error) {
        // Reset still works when storage is unavailable.
      }
    };

    let offset = clamp(readOffset());
    let drag = null;
    const applyOffset = () => {
      wrapper.setAttribute("transform", `translate(${offset.x} ${offset.y})`);
    };
    const localPoint = event => {
      const matrix = coordinateRoot.getScreenCTM();
      return matrix
        ? new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse())
        : null;
    };
    const reset = event => {
      if (event) event.preventDefault();
      offset = {x: 0, y: 0};
      applyOffset();
      removeOffset();
    };

    applyOffset();
    legend.addEventListener("pointerdown", event => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      const point = localPoint(event);
      if (!point) return;
      event.preventDefault();
      legend.focus();
      legend.setPointerCapture(event.pointerId);
      legend.classList.add("is-dragging");
      drag = {
        pointerId: event.pointerId,
        x: point.x,
        y: point.y,
        offsetX: offset.x,
        offsetY: offset.y
      };
    });
    legend.addEventListener("pointermove", event => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const point = localPoint(event);
      if (!point) return;
      event.preventDefault();
      offset = clamp({
        x: drag.offsetX + point.x - drag.x,
        y: drag.offsetY + point.y - drag.y
      });
      applyOffset();
    });
    const finishDrag = event => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      legend.classList.remove("is-dragging");
      drag = null;
      saveOffset(offset);
    };
    legend.addEventListener("pointerup", finishDrag);
    legend.addEventListener("pointercancel", finishDrag);
    legend.addEventListener("dblclick", reset);
    legend.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        reset(event);
        return;
      }
      const movement = {
        ArrowLeft: [-1, 0],
        ArrowRight: [1, 0],
        ArrowUp: [0, -1],
        ArrowDown: [0, 1]
      }[event.key];
      if (!movement) return;
      event.preventDefault();
      const step = event.shiftKey ? 10 : 2;
      offset = clamp({
        x: offset.x + movement[0] * step,
        y: offset.y + movement[1] * step
      });
      applyOffset();
      saveOffset(offset);
    });
  });
})();
</script>"""


def build_html(sheets: Mapping[str, pd.DataFrame], xlsx: Path,
               sections=ALL_SECTIONS) -> str:
    sections = set(sections)
    mode = result_mode(sheets)
    tag = mode.upper()
    mode_label = "Arbitrage" if mode == "arb" else "Self-consumption"
    summ = sheets["Summary"].iloc[0]
    cash = sheets["Cashflow_Timeline"]
    cum_disc_col = column(cash.columns, "cum_discounted", tag)
    horizon = int(cash["Year"].max())
    payback = next(
        (int(y) for y, c in zip(cash["Year"], cash[cum_disc_col]) if c >= 0),
        None,
    )
    year_one = cash.loc[cash["Year"] == 1, "Discount_factor"]
    npv_sub = "net present value"
    if not year_one.empty:
        npv_sub += f" @{float(year_one.iloc[0]) - 1:.1%}"

    avg_savings_col = column(summ.index, "avg_savings", tag)
    npv_col = column(summ.index, "npv", tag)
    self_supply_col = (
        "Weighted_load_self_supply_rate" if mode == "arb"
        else "Weighted_self_sufficiency_rate"
    )
    energy_import_col = (
        "Project_total_grid_import_kWh" if mode == "arb"
        else "Project_grid_import_kWh"
    )
    energy_export_col = (
        "Project_total_grid_export_kWh" if mode == "arb"
        else "Project_grid_export_kWh"
    )

    svg = ""
    if "charts" in sections:
        fig = build(sheets, xlsx)
        fig.suptitle("")  # drop the in-figure banner; HTML tiles carry it
        svg = fig_to_svg(fig)

    tiles = "".join([
        tile(f"NPV ({horizon} y)", eur(summ[npv_col]), npv_sub),
        tile("Avg savings", eur(summ[avg_savings_col]) + "/y",
              "PV+BESS effect, both at project load"),
        tile("Discounted payback", f"{payback} y" if payback is not None else f">{horizon} y"),
        tile("CAPEX", eur(summ["CAPEX_total_new_EUR"]),
             f'PV {eur(summ["CAPEX_PV_new_EUR"])} + BESS {eur(summ["CAPEX_BESS_new_EUR"])}'),
        tile("PV generation", kwh(summ[column(summ.index, "total_pv", tag)] / 1000, "MWh"), f"{horizon}-year total"),
        tile("Self-supply", pct(summ[self_supply_col]), "load covered by PV+BESS"),
        tile("Grid import", kwh(summ[column(summ.index, "total_import", tag)] / 1000, "MWh"), f"{horizon}-year total"),
        tile("Grid export", kwh(summ[column(summ.index, "total_export", tag)] / 1000, "MWh"), f"{horizon}-year total"),
    ])

    body = ""
    if "charts" in sections and svg:
        body += ('<section><h2>Charts</h2>'
                 '<p class="chart-help">Drag legends within each chart; '
                 'double-click to reset.</p>'
                 f'<div class="chart">{svg}</div></section>\n')
        body += '''<section><h2>Chart glossary</h2><dl class="glossary">
<dt>Baseline load</dt><dd>Consumers plus existing EV demand.</dd>
<dt>Project load</dt><dd>Consumers plus EV demand after planned adoption.</dd>
<dt>Grid only</dt><dd>Demand supplied from grid without PV or BESS.</dd>
<dt>Existing assets</dt><dd>PV and BESS already installed before project.</dd>
<dt>Project assets</dt><dd>Existing assets plus planned PV and BESS.</dd>
<dt>PV+BESS effect</dt><dd>Project-assets savings versus existing assets at same project load.</dd>
</dl></section>\n'''
    if "economics" in sections:
        econ_t = table(sheets["Economics"])
        body += f'<section><h2>Economics by year</h2><div class="tablewrap">{econ_t}</div></section>\n'
    if "energy" in sections:
        self_supply_rate_col = (
            "Load_self_supply_rate" if mode == "arb"
            else "Project_self_sufficiency_rate"
        )
        ebal_cols = ["Year", "Project_load_kWh", "PV_project_generation_kWh",
                     energy_import_col, energy_export_col, self_supply_rate_col]
        ebal_t = table(sheets["Energy_Balance"][ebal_cols])
        body += f'<section><h2>Energy balance by year</h2><div class="tablewrap">{ebal_t}</div></section>\n'

    # Grid-constraint screening block — always shown when present (folded into the
    # report as a separate block, per review decision).
    if "Grid_Screening" in sheets and not sheets["Grid_Screening"].empty:
        body += screening_section(sheets["Grid_Screening"], summ)

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PV + BESS {mode_label} — {horizon}-year report</title>
<style>
  :root {{ --bg:#f6f7f9; --card:#fff; --ink:#1c2530; --muted:#66707c;
           --accent:#1f77b4; --line:#e3e7ec; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
          font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:1180px; margin:0 auto; padding:32px 20px 64px; }}
  header h1 {{ margin:0 0 4px; font-size:24px; }}
  header .meta {{ color:var(--muted); font-size:13px; }}
  .tiles {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin:24px 0; }}
  @media (max-width:760px) {{ .tiles {{ grid-template-columns:repeat(2,1fr); }} }}
  .tile {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:16px 18px; }}
  .tile-val {{ font-size:22px; font-weight:700; }}
  .tile-label {{ color:var(--muted); font-size:12px; margin-top:2px;
                 text-transform:uppercase; letter-spacing:.04em; }}
  .tile-sub {{ color:var(--muted); font-size:12px; margin-top:6px; }}
  section {{ background:var(--card); border:1px solid var(--line); border-radius:14px;
             padding:20px 22px; margin:20px 0; }}
  section h2 {{ margin:0 0 14px; font-size:17px; }}
  .chart {{ width:100%; overflow-x:auto; }}
  .chart svg {{ max-width:100%; height:auto; }}
  .chart-help {{ color:var(--muted); font-size:12px; margin:-8px 0 10px; }}
  .chart svg .draggable-legend {{ cursor:grab; touch-action:none; user-select:none; }}
  .chart svg .draggable-legend.is-dragging {{ cursor:grabbing; }}
  table.data {{ border-collapse:collapse; width:100%; font-size:13px; }}
  table.data th, table.data td {{ text-align:right; padding:6px 10px;
             border-bottom:1px solid var(--line); white-space:nowrap; }}
  table.data th {{ color:var(--muted); font-weight:600; position:sticky; top:0;
             background:var(--card); }}
  table.data td:first-child, table.data th:first-child {{ text-align:left; }}
  .tablewrap {{ overflow-x:auto; }}
  .verdict {{ display:flex; flex-wrap:wrap; gap:8px 18px; align-items:center; margin-bottom:8px; font-size:14px; }}
  .vsub {{ color:var(--muted); font-size:13px; }}
  .note {{ color:var(--muted); font-size:12px; margin:4px 0 12px; }}
  .glossary {{ display:grid; grid-template-columns:max-content 1fr; gap:7px 16px; margin:0; }}
  .glossary dt {{ font-weight:700; }} .glossary dd {{ margin:0; color:var(--muted); }}
  footer {{ color:var(--muted); font-size:12px; margin-top:28px; text-align:center; }}
</style></head><body><div class="wrap">
<header>
  <h1>PV + BESS {mode_label} — {horizon}-year techno-economic report</h1>
  <div class="meta">source: {html.escape(xlsx.name)} &nbsp;·&nbsp; generated {stamp}</div>
</header>

<div class="tiles">{tiles}</div>

{body}
<footer>EComTool · Python analysis · Detailed diagnostics available in Results XLSX.</footer>
{DRAGGABLE_LEGENDS_SCRIPT}
</div></body></html>"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("xlsx", nargs="?", default=str(DEFAULT_XLSX))
    p.add_argument("-o", "--out", default=str(DEFAULT_OUT))
    args = p.parse_args()

    xlsx = Path(args.xlsx)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    sheets = load(xlsx)
    out.write_text(build_html(sheets, xlsx), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
