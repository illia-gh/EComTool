"""PCC grid-constraint screening: voltage deviation at the point of common coupling.

A fast, peak-based estimate at the PCC, assuming a 3-phase 400 V line-to-line
connection. This is NOT a time-series power flow: for each screened year only the
two worst instants are evaluated — peak import (largest voltage drop) and peak
export (largest voltage rise).

For net power P (kW) and an estimated reactive power Q = P*tan(acos(pf)):

    S     = sqrt(P^2 + Q^2)                        [kVA]
    I     = 1000*S_kVA / (sqrt(3) * V_base)        [A]   (3-phase, V_base = 400 V)
    dV    = (R*P + X*Q) / V_base^2                 [p.u.]  (signed: import <0, export >0)
    V_PCC = V_grid_pu + dV                          [p.u.]

The docx voltage-drop formula (R*P + X*Q)/V yields volts; normalising once more by
V_base expresses it in per-unit, comparable to the 0.95-1.05 p.u. limits. dV is
reported signed so that V_PCC = V_grid + dV holds for both directions (import
lowers the voltage, export raises it). See docs/grid-screening-plan.md.
"""
from __future__ import annotations

import math

V_BASE_V = 400.0                 # 3-phase line-to-line nominal (PCC)
_SQRT3 = math.sqrt(3.0)

# Thévenin R/X (ohms) from the PCC to a stiff source, per connection type (docx).
IMPEDANCE = {
    "urban":    (0.05, 0.02),
    "suburban": (0.15, 0.06),
    "rural":    (0.40, 0.15),
}


def impedance(connection_type: str) -> tuple[float, float]:
    """Return (R, X) in ohms for a connection type."""
    try:
        return IMPEDANCE[str(connection_type).strip().lower()]
    except KeyError as exc:
        raise ValueError(
            f"unknown connection_type {connection_type!r}; expected {sorted(IMPEDANCE)}"
        ) from exc


def screen_point(p_kW: float, R: float, X: float, pf: float,
                 v_grid_pu: float, *, is_export: bool) -> dict:
    """Screen a single peak instant. Returns Q, S (kVA), I (A), signed dV and V_PCC.

    dV is negative for import (voltage drop) and positive for export (voltage rise),
    so V_PCC = v_grid + dV in both cases.
    """
    if not math.isfinite(pf) or not 0.0 < pf <= 1.0:
        raise ValueError(f"power factor must be finite and in (0, 1], got {pf!r}")
    tan_phi = math.tan(math.acos(pf))
    q_kvar = p_kW * tan_phi
    s_kVA = math.hypot(p_kW, q_kvar)
    i_A = s_kVA * 1000.0 / (_SQRT3 * V_BASE_V)          # 3-phase: I = S / (√3·V_LL)
    # R,X in ohms; P,Q converted kW/kvar -> W/var; divide by V_base^2 for p.u.
    dv_mag = (R * p_kW * 1000.0 + X * q_kvar * 1000.0) / (V_BASE_V ** 2)
    dv_pu = dv_mag if is_export else -dv_mag
    return {"Q_kvar": q_kvar, "S_kVA": s_kVA, "I_A": i_A,
            "dV_pu": dv_pu, "V_PCC_pu": v_grid_pu + dv_pu}


def milestone_years(years: list[int], wanted: list[int]) -> list[int]:
    """Select the milestone years to screen: the requested years present in the
    run (clamped to the horizon), with the final year always included, sorted."""
    horizon = max(years)
    present = set(int(y) for y in years)
    sel = {int(w) for w in (wanted or []) if int(w) in present}
    sel.add(int(horizon))
    return sorted(sel)
