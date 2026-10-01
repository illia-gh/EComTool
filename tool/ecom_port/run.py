"""Compute Python SC/ARB results and optionally export a Results workbook.

Calculation returns an in-memory :class:`ResultBundle`. XLSX writing remains a
compatibility boundary for CLI/MATLAB workflows and explicit user exports.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from types import MappingProxyType
from typing import Callable, Mapping
from uuid import uuid4

import openpyxl
import pandas as pd
from openpyxl.writer.excel import ExcelWriter
from zipfile import ZIP_DEFLATED, ZipFile

from . import results as R
from .frontend import Inputs, build_state, load_config, read_output, read_prices

TimingCallback = Callable[[str, float], None]


def _record(on_timing: TimingCallback | None, label: str, started: float) -> None:
    if on_timing is not None:
        on_timing(label, perf_counter() - started)

# Same order as export_pv_bess_sc_results.m writetable calls.
SC_SHEET_ORDER = [
    "Summary", "Energy_Balance", "Load_Profiles", "SC_Flows", "Economics",
    "Price_Annual", "Price_TimeSeries", "BESS_Degradation", "Cashflow_Timeline",
    "Grid_Screening",
]

# Same order as export_pv_bess_arb_results.m writetable calls (SC_Flows -> ARB_Flows).
ARB_SHEET_ORDER = [
    "Summary", "Energy_Balance", "Load_Profiles", "ARB_Flows", "Economics",
    "Price_Annual", "Price_TimeSeries", "BESS_Degradation", "Cashflow_Timeline",
    "Grid_Screening",
]


def sheet_order(mode: str) -> tuple[str, ...]:
    """Return canonical workbook order for one calculation mode."""
    normalized = mode.lower()
    if normalized == "sc":
        return tuple(SC_SHEET_ORDER)
    if normalized == "arb":
        return tuple(ARB_SHEET_ORDER)
    raise ValueError(f"unknown result mode {mode!r} (expected 'sc' or 'arb')")


@dataclass(frozen=True)
class ResultBundle:
    """Complete calculation result snapshot shared by report and exporters.

    Mapping shape is immutable. DataFrames are treated as read-only after
    publication so report rendering and background export can safely share one
    snapshot without copying the large ``Price_TimeSeries`` table.
    """

    mode: str
    sheets: Mapping[str, pd.DataFrame]

    def __post_init__(self) -> None:
        normalized = self.mode.lower()
        expected = sheet_order(normalized)
        missing = [name for name in expected if name not in self.sheets]
        if missing:
            raise ValueError(f"ResultBundle missing sheets: {missing}")
        object.__setattr__(self, "mode", normalized)
        object.__setattr__(self, "sheets", MappingProxyType(dict(self.sheets)))

    @property
    def order(self) -> tuple[str, ...]:
        return sheet_order(self.mode)


def validate_sc_config(cfg: dict) -> None:
    """Reject MATLAB SC branches that have not been ported yet."""
    if not cfg.get("fast_sc_dispatch", True):
        raise NotImplementedError(
            "Python SC engine supports only fast_sc_dispatch=true; "
            "use MATLAB for LP-backed SC dispatch"
        )


def compute_sc(output_xlsx: Path, price_xlsx: Path,
               config_path: Path | None, defaults_path: Path,
               *, inputs: Inputs | None = None,
               on_timing: TimingCallback | None = None) -> ResultBundle:
    started = perf_counter()
    cfg = load_config(config_path, defaults_path)
    validate_sc_config(cfg)
    inp = inputs if inputs is not None else read_output(output_xlsx)
    spot = read_prices(price_xlsx, inp.years)
    st = build_state(inp, spot, cfg)
    _record(on_timing, "load config/prices and build state", started)
    started = perf_counter()
    ctx = R.run_scenarios(st)
    _record(on_timing, "SC dispatch scenarios", started)
    proj = ctx["proj"]
    started = perf_counter()
    sheets = {
        "Summary": R.summary_df(st, ctx),
        "Energy_Balance": R.energy_balance_df(st, proj),
        "Load_Profiles": R.load_profiles_df(st),
        "SC_Flows": R.sc_flows_df(st, proj),
        "Economics": R.economics_df(st, ctx),
        "Price_Annual": R.price_annual_df(st),
        "Price_TimeSeries": R.price_timeseries_df(st),
        "BESS_Degradation": R.bess_degradation_df(inp, cfg),
        "Cashflow_Timeline": R.cashflow_timeline_df(st, ctx, "sc"),
        "Grid_Screening": R.grid_screening_df(st, proj, cfg),
    }
    _record(on_timing, "build result tables", started)
    return ResultBundle("sc", sheets)


def build_sc_sheets(output_xlsx: Path, price_xlsx: Path,
                    config_path: Path | None, defaults_path: Path,
                    *, inputs: Inputs | None = None,
                    on_timing: TimingCallback | None = None) -> dict[str, pd.DataFrame]:
    """Compatibility API returning the historical mutable sheet dictionary."""
    return dict(compute_sc(
        output_xlsx, price_xlsx, config_path, defaults_path,
        inputs=inputs, on_timing=on_timing,
    ).sheets)


def write_results(sheets: Mapping[str, pd.DataFrame], out_path: Path,
                  order: list[str] | tuple[str, ...]) -> None:
    """Write complete workbook atomically, preserving previous result on failure.

    Uses openpyxl write_only mode and low-cost ZIP compression: the big
    Price_TimeSeries sheet (up to ~700k rows) is streamed instead of built as an
    in-memory cell grid. Raises KeyError before touching the destination if a
    requested sheet is missing.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = out_path.with_name(f".{out_path.stem}.{uuid4().hex}.tmp{out_path.suffix}")
    try:
        frames = [(name, sheets[name]) for name in order]   # KeyError here = no partial write
        wb = openpyxl.Workbook(write_only=True)
        for name, df in frames:
            ws = wb.create_sheet(title=name)
            ws.append(list(df.columns))
            for row in df.itertuples(index=False, name=None):
                ws.append(row)
        archive = ZipFile(
            temp_path, "w", ZIP_DEFLATED, allowZip64=True, compresslevel=1
        )
        try:
            ExcelWriter(wb, archive).save()
        finally:
            archive.close()
        temp_path.replace(out_path)
    finally:
        temp_path.unlink(missing_ok=True)


def export_results_xlsx(result: ResultBundle, output_path: Path) -> None:
    """Serialize one immutable result snapshot to XLSX."""
    write_results(result.sheets, output_path, result.order)


def run_sc(output_xlsx: Path, price_xlsx: Path, config_path: Path | None,
           defaults_path: Path, out_path: Path, *, inputs: Inputs | None = None,
           on_timing: TimingCallback | None = None) -> None:
    result = compute_sc(
        output_xlsx, price_xlsx, config_path, defaults_path,
        inputs=inputs, on_timing=on_timing,
    )
    started = perf_counter()
    export_results_xlsx(result, out_path)
    _record(on_timing, "write Results XLSX", started)


def compute_arb(output_xlsx: Path, price_xlsx: Path,
                config_path: Path | None, defaults_path: Path,
                *, inputs: Inputs | None = None,
                on_timing: TimingCallback | None = None) -> ResultBundle:
    started = perf_counter()
    cfg = load_config(config_path, defaults_path)
    inp = inputs if inputs is not None else read_output(output_xlsx)
    spot = read_prices(price_xlsx, inp.years)
    st = build_state(inp, spot, cfg)
    _record(on_timing, "load config/prices and build state", started)
    started = perf_counter()
    ctx = R.run_scenarios_arb(st)
    _record(on_timing, "ARB dispatch scenarios", started)
    proj = ctx["proj"]
    started = perf_counter()
    sheets = {
        "Summary": R.summary_arb_df(st, ctx),
        "Energy_Balance": R.energy_balance_arb_df(st, proj),
        "Load_Profiles": R.load_profiles_df(st),
        "ARB_Flows": R.arb_flows_df(st, proj),
        "Economics": R.economics_arb_df(st, ctx),
        "Price_Annual": R.price_annual_df(st),
        "Price_TimeSeries": R.price_timeseries_df(st),
        "BESS_Degradation": R.bess_degradation_df(inp, cfg),
        "Cashflow_Timeline": R.cashflow_timeline_df(st, ctx, "arb"),
        "Grid_Screening": R.grid_screening_df(st, proj, cfg),
    }
    _record(on_timing, "build result tables", started)
    return ResultBundle("arb", sheets)


def build_arb_sheets(output_xlsx: Path, price_xlsx: Path,
                     config_path: Path | None, defaults_path: Path,
                     *, inputs: Inputs | None = None,
                     on_timing: TimingCallback | None = None) -> dict[str, pd.DataFrame]:
    """Compatibility API returning the historical mutable sheet dictionary."""
    return dict(compute_arb(
        output_xlsx, price_xlsx, config_path, defaults_path,
        inputs=inputs, on_timing=on_timing,
    ).sheets)


def run_arb(output_xlsx: Path, price_xlsx: Path, config_path: Path | None,
            defaults_path: Path, out_path: Path, *, inputs: Inputs | None = None,
            on_timing: TimingCallback | None = None) -> None:
    result = compute_arb(
        output_xlsx, price_xlsx, config_path, defaults_path,
        inputs=inputs, on_timing=on_timing,
    )
    started = perf_counter()
    export_results_xlsx(result, out_path)
    _record(on_timing, "write Results XLSX", started)
