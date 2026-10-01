"""ARB-mode battery dispatch, ported from optimize_arb_flow_horizon.m +
simulate_20y_arb.m + baseline_grid_only_arb_20y.m.

Flow-based price arbitrage with a rolling-horizon LP: optimise the next
`arb_horizon_hours` (default 48 h) minimising import cost - export revenue +
small throughput penalty, then commit only the first `arb_apply_hours`
(default 24 h) and roll forward carrying SOC. Unlike SC dispatch the battery may
charge from the grid (Grid_to_BESS) and export to the grid (BESS_to_grid), and
PV may be curtailed. Monthly net-billing wallet is identical to SC.

Parity is a sanity check, not a hard contract (see docs/MODEL-SPEC.md): the LP
can be degenerate (export vs curtail ties) so per-interval flows may differ from
MATLAB's HiGHS while the economics match.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csr_matrix

DT_H = 0.25
STEPS_PER_DAY = 96
DAYS_PER_YEAR = 365


def _net_pcc_peaks_kW(eimp_kWh: np.ndarray, eexp_kWh: np.ndarray) -> tuple[float, float]:
    """Return peak net PCC import/export power from per-step directional energy."""
    eimp = np.asarray(eimp_kWh, dtype=float)
    eexp = np.asarray(eexp_kWh, dtype=float)
    if eimp.shape != eexp.shape:
        raise ValueError("eimp/eexp must have the same shape")
    if eimp.size == 0:
        return 0.0, 0.0
    p_net_kW = (eimp - eexp) / DT_H
    return (
        max(float(p_net_kW.max()), 0.0),
        max(float((-p_net_kW).max()), 0.0),
    )


# Flow field names, order matching simulate_20y_arb.m flowNames.
FLOW_NAMES = ("PV_to_load", "PV_to_BESS", "PV_to_grid", "PV_curtailed",
              "Grid_to_load", "Grid_to_BESS", "BESS_to_load", "BESS_to_grid")
ORIGIN_FLOW_NAMES = ("BESS_PV_to_load", "BESS_PV_to_grid",
                     "BESS_grid_to_load", "BESS_grid_to_grid")


def _make_batt(cap_kWh: float, power_kW: float, DoD: float,
               eta_ch: float, eta_dis: float) -> dict:
    Emax = cap_kWh
    Emin = (1 - DoD) * cap_kWh
    return {"Emax": Emax, "Emin": Emin, "SOC0": 0.5 * (Emax + Emin),
            "Pch": power_kW, "Pdis": power_kW, "eta_ch": eta_ch, "eta_dis": eta_dis}


def _dispatch_without_bess(pv_kW: np.ndarray, load_kWh: np.ndarray,
                           cbuy: np.ndarray, csell: np.ndarray,
                           soc_start: float, Emin: float, Emax: float,
                           apply_steps: int, curtail_penalty: float) -> dict | None:
    """Return exact zero-power flows, or None when LP tie semantics are needed."""
    pv_kW = np.asarray(pv_kW, float).ravel()
    load_kWh = np.asarray(load_kWh, float).ravel()
    cbuy = np.asarray(cbuy, float).ravel()
    csell = np.asarray(csell, float).ravel()
    T = pv_kW.size
    if not (load_kWh.size == cbuy.size == csell.size == T):
        raise ValueError("pv/load/cbuy/csell must have the same length")
    apply_steps = min(apply_steps if apply_steps else T, T)

    # k == 0 leaves PV-to-load basis-dependent in HiGHS. Preserve current
    # result semantics by letting caller fall back to LP for that committed day.
    k_gap = cbuy[:apply_steps] + np.minimum(-csell[:apply_steps], curtail_penalty)
    if np.any(k_gap == 0.0):
        return None

    Epv = pv_kW[:apply_steps] * DT_H
    Eload = load_kWh[:apply_steps]
    pv_to_load = np.where(k_gap > 0.0, np.minimum(Epv, Eload), 0.0)
    remaining_pv = Epv - pv_to_load
    # Stable HiGHS convention for export/curtail ties: prefer export.
    pv_to_grid = np.where(-csell[:apply_steps] <= curtail_penalty,
                          remaining_pv, 0.0)
    pv_curtailed = remaining_pv - pv_to_grid
    grid_to_load = Eload - pv_to_load
    zeros = np.zeros(apply_steps)

    out = {
        "PV_to_load": pv_to_load, "PV_to_BESS": zeros.copy(),
        "PV_to_grid": pv_to_grid, "PV_curtailed": pv_curtailed,
        "Grid_to_load": grid_to_load, "Grid_to_BESS": zeros.copy(),
        "BESS_to_load": zeros.copy(), "BESS_to_grid": zeros.copy(),
        "Eimp": grid_to_load, "Eexp": pv_to_grid,
        "Ech": zeros.copy(), "Edis": zeros.copy(), "Ecurt": pv_curtailed,
        "soc_end": min(max(soc_start, Emin), Emax),
    }
    cb, cs = cbuy[:apply_steps], csell[:apply_steps]
    out["import_cost"] = float((cb * out["Eimp"]).sum())
    out["export_revenue"] = float((cs * out["Eexp"]).sum())
    out["grid_to_BESS_cost"] = 0.0
    out["BESS_to_grid_revenue"] = 0.0
    out["PV_to_grid_revenue"] = out["export_revenue"]
    return out


def optimize_arb_flow_horizon(pv_kW: np.ndarray, load_kWh: np.ndarray,
                              cbuy: np.ndarray, csell: np.ndarray, batt: dict,
                              soc_start: float, apply_steps: int,
                              cycle_cost: float = 1e-6,
                              curtail_penalty: float = 0.0,
                              max_cycles_per_day: float = np.inf) -> dict:
    """Solve one horizon LP; return committed flows for the first apply_steps.

    Decision vars per interval: [PV2L, PV2B, PV2G, PVcurt, G2L, G2B, B2L, B2G]
    plus SOC over T+1 nodes. Mirrors optimize_arb_flow_horizon.m exactly.
    """
    pv_kW = np.asarray(pv_kW, float).ravel()
    load_kWh = np.asarray(load_kWh, float).ravel()
    cbuy = np.asarray(cbuy, float).ravel()
    csell = np.asarray(csell, float).ravel()

    T = pv_kW.size
    if not (load_kWh.size == cbuy.size == csell.size == T):
        raise ValueError("pv/load/cbuy/csell must have the same length")
    apply_steps = min(apply_steps if apply_steps else T, T)

    Epv = pv_kW * DT_H
    Eload = load_kWh
    Ech_max = batt["Pch"] * DT_H
    Edis_max = batt["Pdis"] * DT_H
    eta_ch, eta_dis = batt["eta_ch"], batt["eta_dis"]
    Emin, Emax = batt["Emin"], batt["Emax"]

    nE = T
    nSOC = T + 1
    # variable-block offsets
    PV2L = np.arange(nE)
    PV2B = nE + np.arange(nE)
    PV2G = 2 * nE + np.arange(nE)
    PVcurt = 3 * nE + np.arange(nE)
    G2L = 4 * nE + np.arange(nE)
    G2B = 5 * nE + np.arange(nE)
    B2L = 6 * nE + np.arange(nE)
    B2G = 7 * nE + np.arange(nE)
    SOC = 8 * nE + np.arange(nSOC)
    n = 8 * nE + nSOC

    trow = np.arange(T)
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[np.ndarray] = []
    beq_parts: list[np.ndarray] = []
    row0 = 0

    def add_eq(cc_blocks, vv_blocks, beq):
        nonlocal row0
        k = len(cc := cc_blocks)
        rr = np.tile(trow, k) + row0
        rows.append(rr)
        cols.append(np.concatenate(cc))
        vals.append(np.concatenate(vv_blocks))
        beq_parts.append(beq)
        row0 += T

    # PV balance: PV2L+PV2B+PV2G+PVcurt = Epv
    add_eq([PV2L, PV2B, PV2G, PVcurt], [np.ones(T)] * 4, Epv)
    # Load balance: PV2L+G2L+B2L = Eload
    add_eq([PV2L, G2L, B2L], [np.ones(T)] * 3, Eload)
    # SOC dynamics: SOC[t+1]-SOC[t]-eta_ch*(PV2B+G2B)+(B2L+B2G)/eta_dis = 0
    add_eq([SOC[1:], SOC[:-1], PV2B, G2B, B2L, B2G],
           [np.ones(T), -np.ones(T), -eta_ch * np.ones(T), -eta_ch * np.ones(T),
            (1 / eta_dis) * np.ones(T), (1 / eta_dis) * np.ones(T)],
           np.zeros(T))

    # SOC init (single row)
    rows.append(np.array([row0]))
    cols.append(np.array([SOC[0]]))
    vals.append(np.array([1.0]))
    beq_parts.append(np.array([min(max(soc_start, Emin), Emax)]))
    row0 += 1

    Aeq = csr_matrix((np.concatenate(vals),
                      (np.concatenate(rows), np.concatenate(cols))),
                     shape=(row0, n))
    beq = np.concatenate(beq_parts)

    # Inequalities: charge / discharge power limits (+ optional daily cycle cap)
    irows: list[np.ndarray] = []
    icols: list[np.ndarray] = []
    ivals: list[np.ndarray] = []
    b_parts: list[np.ndarray] = []
    irow0 = 0

    def add_le(cc_blocks, vv_blocks, b):
        nonlocal irow0
        k = len(cc_blocks)
        irows.append(np.tile(trow, k) + irow0)
        icols.append(np.concatenate(cc_blocks))
        ivals.append(np.concatenate(vv_blocks))
        b_parts.append(b)
        irow0 += T

    add_le([PV2B, G2B], [np.ones(T)] * 2, Ech_max * np.ones(T))   # charge power
    add_le([B2L, B2G], [np.ones(T)] * 2, Edis_max * np.ones(T))   # discharge power

    if np.isfinite(max_cycles_per_day):
        steps_per_day = round(24 / DT_H)
        n_blocks = int(np.ceil(T / steps_per_day))
        usable = max(Emax - Emin, 0.0) * eta_dis
        br, bc, bv, bb = [], [], [], []
        for ib in range(n_blocks):
            k0 = ib * steps_per_day
            k1 = min(T, (ib + 1) * steps_per_day)
            kk = np.arange(k0, k1)
            br.append(np.full(2 * kk.size, irow0 + ib))
            bc.append(np.concatenate([B2L[kk], B2G[kk]]))
            bv.append(np.ones(2 * kk.size))
            bb.append(max_cycles_per_day * usable)
        irows.append(np.concatenate(br))
        icols.append(np.concatenate(bc))
        ivals.append(np.concatenate(bv))
        b_parts.append(np.asarray(bb))
        irow0 += n_blocks

    if irow0:
        A_ub = csr_matrix((np.concatenate(ivals),
                           (np.concatenate(irows), np.concatenate(icols))),
                          shape=(irow0, n))
        b_ub = np.concatenate(b_parts)
    else:
        A_ub = None
        b_ub = None

    # Bounds
    lb = np.zeros(n)
    ub = np.full(n, np.inf)
    lb[SOC] = Emin
    ub[SOC] = Emax

    # Objective
    f = np.zeros(n)
    f[G2L] = cbuy
    f[G2B] = cbuy
    f[PV2G] = -csell
    f[B2G] = -csell
    f[PV2B] += cycle_cost
    f[G2B] += cycle_cost
    f[B2L] += cycle_cost
    f[B2G] += cycle_cost
    f[PVcurt] = curtail_penalty

    res = linprog(f, A_ub=A_ub, b_ub=b_ub, A_eq=Aeq, b_eq=beq,
                  bounds=np.column_stack([lb, ub]), method="highs")
    if not res.success:
        raise RuntimeError(f"ARB horizon LP failed: {res.message}")
    x = res.x

    k = np.arange(apply_steps)
    out = {
        "PV_to_load": x[PV2L[k]], "PV_to_BESS": x[PV2B[k]],
        "PV_to_grid": x[PV2G[k]], "PV_curtailed": x[PVcurt[k]],
        "Grid_to_load": x[G2L[k]], "Grid_to_BESS": x[G2B[k]],
        "BESS_to_load": x[B2L[k]], "BESS_to_grid": x[B2G[k]],
    }
    out["Eimp"] = out["Grid_to_load"] + out["Grid_to_BESS"]
    out["Eexp"] = out["PV_to_grid"] + out["BESS_to_grid"]
    out["Ech"] = out["PV_to_BESS"] + out["Grid_to_BESS"]
    out["Edis"] = out["BESS_to_load"] + out["BESS_to_grid"]
    out["Ecurt"] = out["PV_curtailed"]
    soc_full = x[SOC]
    out["soc_end"] = soc_full[apply_steps]
    cb, cs = cbuy[k], csell[k]
    out["import_cost"] = float((cb * out["Eimp"]).sum())
    out["export_revenue"] = float((cs * out["Eexp"]).sum())
    out["grid_to_BESS_cost"] = float((cb * out["Grid_to_BESS"]).sum())
    out["BESS_to_grid_revenue"] = float((cs * out["BESS_to_grid"]).sum())
    out["PV_to_grid_revenue"] = float((cs * out["PV_to_grid"]).sum())
    return out


def optimize_arb_flow_horizon_by_origin(
        pv_kW: np.ndarray, load_kWh: np.ndarray,
        cbuy: np.ndarray, csell_pv: np.ndarray, csell_grid: np.ndarray,
        batt: dict, soc_pv_start: float, soc_grid_start: float, apply_steps: int,
        cycle_cost: float = 1e-6, curtail_penalty: float = 0.0,
        max_cycles_per_day: float = np.inf) -> dict:
    """Solve Green-Tariff ARB with auditable PV/grid storage provenance.

    Physical battery limits apply to combined inventories. Direct PV export and
    PV-origin battery export use ``csell_pv``; grid-origin battery export uses
    ``csell_grid``. Initial unattributed energy must be supplied in grid ledger.
    """
    pv_kW = np.asarray(pv_kW, float).ravel()
    load_kWh = np.asarray(load_kWh, float).ravel()
    cbuy = np.asarray(cbuy, float).ravel()
    csell_pv = np.asarray(csell_pv, float).ravel()
    csell_grid = np.asarray(csell_grid, float).ravel()
    T = pv_kW.size
    if not (load_kWh.size == cbuy.size == csell_pv.size == csell_grid.size == T):
        raise ValueError("pv/load/cbuy/csell_pv/csell_grid must have the same length")
    apply_steps = min(apply_steps if apply_steps else T, T)

    Epv = pv_kW * DT_H
    Eload = load_kWh
    Ech_max = batt["Pch"] * DT_H
    Edis_max = batt["Pdis"] * DT_H
    eta_ch, eta_dis = batt["eta_ch"], batt["eta_dis"]
    Emin, Emax = batt["Emin"], batt["Emax"]

    nE = T
    nSOC = T + 1
    PV2L = np.arange(nE)
    PV2B = nE + np.arange(nE)
    PV2G = 2 * nE + np.arange(nE)
    PVcurt = 3 * nE + np.arange(nE)
    G2L = 4 * nE + np.arange(nE)
    G2B = 5 * nE + np.arange(nE)
    BPV2L = 6 * nE + np.arange(nE)
    BPV2G = 7 * nE + np.arange(nE)
    BGRID2L = 8 * nE + np.arange(nE)
    BGRID2G = 9 * nE + np.arange(nE)
    SOC_PV = 10 * nE + np.arange(nSOC)
    SOC_GRID = 10 * nE + nSOC + np.arange(nSOC)
    n = 10 * nE + 2 * nSOC

    trow = np.arange(T)
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[np.ndarray] = []
    beq_parts: list[np.ndarray] = []
    row0 = 0

    def add_eq(cc_blocks, vv_blocks, beq):
        nonlocal row0
        rows.append(np.tile(trow, len(cc_blocks)) + row0)
        cols.append(np.concatenate(cc_blocks))
        vals.append(np.concatenate(vv_blocks))
        beq_parts.append(beq)
        row0 += T

    add_eq([PV2L, PV2B, PV2G, PVcurt], [np.ones(T)] * 4, Epv)
    add_eq([PV2L, G2L, BPV2L, BGRID2L], [np.ones(T)] * 4, Eload)
    add_eq([SOC_PV[1:], SOC_PV[:-1], PV2B, BPV2L, BPV2G],
           [np.ones(T), -np.ones(T), -eta_ch * np.ones(T),
            (1 / eta_dis) * np.ones(T), (1 / eta_dis) * np.ones(T)],
           np.zeros(T))
    add_eq([SOC_GRID[1:], SOC_GRID[:-1], G2B, BGRID2L, BGRID2G],
           [np.ones(T), -np.ones(T), -eta_ch * np.ones(T),
            (1 / eta_dis) * np.ones(T), (1 / eta_dis) * np.ones(T)],
           np.zeros(T))

    for variable, value in ((SOC_PV[0], soc_pv_start),
                            (SOC_GRID[0], soc_grid_start)):
        rows.append(np.array([row0]))
        cols.append(np.array([variable]))
        vals.append(np.array([1.0]))
        beq_parts.append(np.array([max(float(value), 0.0)]))
        row0 += 1

    Aeq = csr_matrix((np.concatenate(vals),
                      (np.concatenate(rows), np.concatenate(cols))),
                     shape=(row0, n))
    beq = np.concatenate(beq_parts)

    irows: list[np.ndarray] = []
    icols: list[np.ndarray] = []
    ivals: list[np.ndarray] = []
    b_parts: list[np.ndarray] = []
    irow0 = 0

    def add_le(cc_blocks, vv_blocks, b):
        nonlocal irow0
        irows.append(np.tile(trow, len(cc_blocks)) + irow0)
        icols.append(np.concatenate(cc_blocks))
        ivals.append(np.concatenate(vv_blocks))
        b_parts.append(b)
        irow0 += T

    add_le([PV2B, G2B], [np.ones(T)] * 2, Ech_max * np.ones(T))
    add_le([BPV2L, BPV2G, BGRID2L, BGRID2G], [np.ones(T)] * 4,
           Edis_max * np.ones(T))

    # Shared physical energy band: Emin <= SOC_PV + SOC_grid <= Emax.
    node_rows = np.arange(nSOC)
    irows.append(np.concatenate([node_rows + irow0, node_rows + irow0 + nSOC,
                                 node_rows + irow0, node_rows + irow0 + nSOC]))
    icols.append(np.concatenate([SOC_PV, SOC_PV, SOC_GRID, SOC_GRID]))
    ivals.append(np.concatenate([np.ones(nSOC), -np.ones(nSOC),
                                 np.ones(nSOC), -np.ones(nSOC)]))
    b_parts.append(np.concatenate([np.full(nSOC, Emax), np.full(nSOC, -Emin)]))
    irow0 += 2 * nSOC

    if np.isfinite(max_cycles_per_day):
        steps_per_day = round(24 / DT_H)
        n_blocks = int(np.ceil(T / steps_per_day))
        usable = max(Emax - Emin, 0.0) * eta_dis
        br, bc, bv, bb = [], [], [], []
        discharge_blocks = (BPV2L, BPV2G, BGRID2L, BGRID2G)
        for ib in range(n_blocks):
            k0 = ib * steps_per_day
            k1 = min(T, (ib + 1) * steps_per_day)
            kk = np.arange(k0, k1)
            br.append(np.full(len(discharge_blocks) * kk.size, irow0 + ib))
            bc.append(np.concatenate([block[kk] for block in discharge_blocks]))
            bv.append(np.ones(len(discharge_blocks) * kk.size))
            bb.append(max_cycles_per_day * usable)
        irows.append(np.concatenate(br))
        icols.append(np.concatenate(bc))
        ivals.append(np.concatenate(bv))
        b_parts.append(np.asarray(bb))
        irow0 += n_blocks

    A_ub = csr_matrix((np.concatenate(ivals),
                       (np.concatenate(irows), np.concatenate(icols))),
                      shape=(irow0, n))
    b_ub = np.concatenate(b_parts)

    f = np.zeros(n)
    f[G2L] = cbuy
    f[G2B] = cbuy + cycle_cost
    f[PV2G] = -csell_pv
    f[BPV2G] = -csell_pv + cycle_cost
    f[BGRID2G] = -csell_grid + cycle_cost
    f[PV2B] += cycle_cost
    f[BPV2L] += cycle_cost
    f[BGRID2L] += cycle_cost
    f[PVcurt] = curtail_penalty

    res = linprog(f, A_ub=A_ub, b_ub=b_ub, A_eq=Aeq, b_eq=beq,
                  bounds=(0, None), method="highs")
    if not res.success:
        raise RuntimeError(f"ARB origin horizon LP failed: {res.message}")
    x = res.x

    k = np.arange(apply_steps)
    out = {
        "PV_to_load": x[PV2L[k]], "PV_to_BESS": x[PV2B[k]],
        "PV_to_grid": x[PV2G[k]], "PV_curtailed": x[PVcurt[k]],
        "Grid_to_load": x[G2L[k]], "Grid_to_BESS": x[G2B[k]],
        "BESS_PV_to_load": x[BPV2L[k]], "BESS_PV_to_grid": x[BPV2G[k]],
        "BESS_grid_to_load": x[BGRID2L[k]], "BESS_grid_to_grid": x[BGRID2G[k]],
    }
    out["BESS_to_load"] = out["BESS_PV_to_load"] + out["BESS_grid_to_load"]
    out["BESS_to_grid"] = out["BESS_PV_to_grid"] + out["BESS_grid_to_grid"]
    out["Eimp"] = out["Grid_to_load"] + out["Grid_to_BESS"]
    out["Eexp"] = out["PV_to_grid"] + out["BESS_to_grid"]
    out["Ech"] = out["PV_to_BESS"] + out["Grid_to_BESS"]
    out["Edis"] = out["BESS_to_load"] + out["BESS_to_grid"]
    out["Ecurt"] = out["PV_curtailed"]
    out["soc_pv_end"] = x[SOC_PV[apply_steps]]
    out["soc_grid_end"] = x[SOC_GRID[apply_steps]]
    out["soc_end"] = out["soc_pv_end"] + out["soc_grid_end"]
    cb = cbuy[k]
    sp = csell_pv[k]
    sg = csell_grid[k]
    out["import_cost"] = float((cb * out["Eimp"]).sum())
    out["PV_to_grid_revenue"] = float((sp * out["PV_to_grid"]).sum())
    out["BESS_PV_to_grid_revenue"] = float((sp * out["BESS_PV_to_grid"]).sum())
    out["BESS_grid_to_grid_revenue"] = float((sg * out["BESS_grid_to_grid"]).sum())
    out["BESS_to_grid_revenue"] = (
        out["BESS_PV_to_grid_revenue"] + out["BESS_grid_to_grid_revenue"])
    out["export_revenue"] = out["PV_to_grid_revenue"] + out["BESS_to_grid_revenue"]
    out["grid_to_BESS_cost"] = float((cb * out["Grid_to_BESS"]).sum())
    return out


def simulate_arb(load_kWh: np.ndarray, pv_kW: np.ndarray, cap_y: np.ndarray,
                 power_y: np.ndarray, DoD: float, eta_ch: float, eta_dis: float,
                 buy: np.ndarray, sell: np.ndarray, month: np.ndarray,
                 cfg: dict, sell_ordinary: np.ndarray | None = None,
                 reset_soc_years: np.ndarray | None = None) -> dict:
    """Rolling-horizon ARB dispatch over [Y, N]; per-year aggregates + bill.

    Mirrors simulate_20y_arb.m: per day solve a 48 h horizon LP, commit 24 h,
    carry SOC across days and years (clamped to each year's band). Monthly wallet
    net-billing (credit offsets that month's import cost, leftover burns at year
    end) is identical to SC.
    """
    Y, N = load_kWh.shape
    cap_y = np.asarray(cap_y, dtype=float)
    power_y = np.asarray(power_y, dtype=float)
    if cap_y.shape != (Y,) or power_y.shape != (Y,):
        raise ValueError(f"cap_y/power_y must both have shape {(Y,)}")
    if not np.all(np.isfinite(cap_y)) or not np.all(np.isfinite(power_y)):
        raise ValueError("cap_y/power_y must contain only finite values")
    if np.any(cap_y < 0) or np.any(power_y < 0):
        raise ValueError("cap_y/power_y must be non-negative")
    if reset_soc_years is None:
        reset_soc_years = np.zeros(Y, dtype=bool)
    else:
        reset_soc_years = np.asarray(reset_soc_years, dtype=bool)
        if reset_soc_years.shape != (Y,):
            raise ValueError(f"reset_soc_years must have shape {(Y,)}")
    track_origin = sell_ordinary is not None
    if track_origin:
        sell_ordinary = np.asarray(sell_ordinary, dtype=float)
        if sell_ordinary.shape != sell.shape:
            raise ValueError(
                f"sell_ordinary shape {sell_ordinary.shape} != sell shape {sell.shape}")
        if not np.all(np.isfinite(sell_ordinary)):
            raise ValueError("sell_ordinary must contain only finite values")
    batts = [_make_batt(cap_y[y], power_y[y], DoD, eta_ch, eta_dis) for y in range(Y)]

    horizon_steps = round(cfg["arb_horizon_hours"] / DT_H)
    apply_steps = round(cfg["arb_apply_hours"] / DT_H)
    if apply_steps != STEPS_PER_DAY:
        raise ValueError("arb_apply_hours must be 24 h for monthly accounting")
    if horizon_steps < apply_steps:
        raise ValueError("arb_horizon_hours must be >= arb_apply_hours")
    cycle_cost = cfg.get("cycle_cost", 1e-6)
    curtail_penalty = cfg.get("arb_curtailment_penalty_EUR_per_kWh", 0.0)
    max_cycles = cfg.get("max_cycles_per_day", np.inf)
    if max_cycles is None:
        max_cycles = np.inf
    reset_soc = cfg.get("reset_soc_each_year", False)

    day_month = month[np.arange(DAYS_PER_YEAR) * STEPS_PER_DAY] - 1   # 0..11 per day

    yImport = np.zeros(Y); yExport = np.zeros(Y)
    yCharge = np.zeros(Y); yDischarge = np.zeros(Y)
    yCurtail = np.zeros(Y); yLoad = np.zeros(Y); yPV = np.zeros(Y)
    yBill = np.zeros(Y); yImportCost = np.zeros(Y)
    yCredit = np.zeros(Y); yWalletBurn = np.zeros(Y)
    ySOCStart = np.zeros(Y)
    yGridToBESSCost = np.zeros(Y)
    yBESSToGridRev = np.zeros(Y)
    yBESSPVToGridRev = np.zeros(Y)
    yBESSGridToGridRev = np.zeros(Y)
    yPVToGridRev = np.zeros(Y)
    yFlows = {fn: np.zeros(Y) for fn in FLOW_NAMES}
    yOriginFlows = {fn: np.zeros(Y) for fn in ORIGIN_FLOW_NAMES}
    # Peak net grid power at PCC (kW) for grid-constraint screening.
    yPeakImport = np.zeros(Y); yPeakExport = np.zeros(Y)

    soc = batts[0]["SOC0"]
    soc_pv = 0.0
    soc_grid = soc
    for y in range(Y):
        b = batts[y]
        reset_this_year = reset_soc or reset_soc_years[y]
        if track_origin:
            if reset_this_year:
                soc_pv, soc_grid = 0.0, b["SOC0"]
            else:
                total = soc_pv + soc_grid
                target = min(max(total, b["Emin"]), b["Emax"])
                if target < total and total > 0:
                    scale = target / total
                    soc_pv *= scale
                    soc_grid *= scale
                elif target > total:
                    # Capacity-band growth injects unattributed reserve in legacy model.
                    # Keep it in non-Green ledger so it can never earn Green Tariff.
                    soc_grid += target - total
            soc = soc_pv + soc_grid
        else:
            soc = (b["SOC0"] if reset_this_year
                   else min(max(soc, b["Emin"]), b["Emax"]))
        ySOCStart[y] = soc
        yLoad[y] = load_kWh[y].sum()
        yPV[y] = pv_kW[y].sum() * DT_H
        import_cost_m = np.zeros(12)
        credit_m = np.zeros(12)
        for d in range(DAYS_PER_YEAR):
            k0 = d * STEPS_PER_DAY
            k1 = k0 + STEPS_PER_DAY
            kH1 = min(N, k0 + horizon_steps)
            out = None
            if power_y[y] == 0.0:
                out = _dispatch_without_bess(
                    pv_kW[y, k0:kH1], load_kWh[y, k0:kH1],
                    buy[y, k0:kH1], sell[y, k0:kH1],
                    soc, b["Emin"], b["Emax"], k1 - k0, curtail_penalty)
                if out is not None and track_origin:
                    zeros = np.zeros(k1 - k0)
                    for name in ORIGIN_FLOW_NAMES:
                        out[name] = zeros.copy()
                    out["soc_pv_end"] = soc_pv
                    out["soc_grid_end"] = soc_grid
                    out["BESS_PV_to_grid_revenue"] = 0.0
                    out["BESS_grid_to_grid_revenue"] = 0.0
            if out is None:
                if track_origin:
                    out = optimize_arb_flow_horizon_by_origin(
                        pv_kW[y, k0:kH1], load_kWh[y, k0:kH1],
                        buy[y, k0:kH1], sell[y, k0:kH1],
                        sell_ordinary[y, k0:kH1], b, soc_pv, soc_grid,
                        k1 - k0, cycle_cost, curtail_penalty, max_cycles)
                else:
                    out = optimize_arb_flow_horizon(
                        pv_kW[y, k0:kH1], load_kWh[y, k0:kH1],
                        buy[y, k0:kH1], sell[y, k0:kH1],
                        b, soc, k1 - k0, cycle_cost, curtail_penalty, max_cycles)
            soc = out["soc_end"]
            if track_origin:
                soc_pv = out["soc_pv_end"]
                soc_grid = out["soc_grid_end"]
            yImport[y] += out["Eimp"].sum()
            yExport[y] += out["Eexp"].sum()
            yCharge[y] += out["Ech"].sum()
            yDischarge[y] += out["Edis"].sum()
            yCurtail[y] += out["Ecurt"].sum()
            # Billing keeps MATLAB-compatible gross Eimp/Eexp. PCC screening uses
            # physical net exchange because ARB can import and export simultaneously.
            peak_imp, peak_exp = _net_pcc_peaks_kW(out["Eimp"], out["Eexp"])
            yPeakImport[y] = max(yPeakImport[y], peak_imp)
            yPeakExport[y] = max(yPeakExport[y], peak_exp)
            yGridToBESSCost[y] += out["grid_to_BESS_cost"]
            yBESSToGridRev[y] += out["BESS_to_grid_revenue"]
            yBESSPVToGridRev[y] += out.get("BESS_PV_to_grid_revenue", 0.0)
            yBESSGridToGridRev[y] += out.get("BESS_grid_to_grid_revenue", 0.0)
            yPVToGridRev[y] += out["PV_to_grid_revenue"]
            for fn in FLOW_NAMES:
                yFlows[fn][y] += out[fn].sum()
            for fn in ORIGIN_FLOW_NAMES:
                if fn in out:
                    yOriginFlows[fn][y] += out[fn].sum()
            m = day_month[d]
            import_cost_m[m] += out["import_cost"]
            credit_m[m] += out["export_revenue"]

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

    result = {
        "yImport": yImport, "yExport": yExport, "yCharge": yCharge,
        "yDischarge": yDischarge, "yCurtail": yCurtail, "yLoad": yLoad, "yPV": yPV,
        "yBill": yBill, "yImportCost": yImportCost, "yCredit": yCredit,
        "yWalletBurn": yWalletBurn, "yGridToBESSCost": yGridToBESSCost,
        "yBESSToGridRev": yBESSToGridRev, "yPVToGridRev": yPVToGridRev,
        "yBESSPVToGridRev": yBESSPVToGridRev,
        "yBESSGridToGridRev": yBESSGridToGridRev,
        "max_cycles_per_day": max_cycles,
        "ySOCStart": ySOCStart,
        "yPeakImport": yPeakImport, "yPeakExport": yPeakExport,
    }
    for fn in FLOW_NAMES:
        result["year" + fn] = yFlows[fn]
    for fn in ORIGIN_FLOW_NAMES:
        result["year" + fn] = yOriginFlows[fn]
    return result


def grid_only_bill_y(buy: np.ndarray, load_grid: np.ndarray) -> np.ndarray:
    """Grid-only baseline: no PV/BESS, no wallet -> sum(buy * load) per year."""
    return (buy * load_grid).sum(axis=1)
