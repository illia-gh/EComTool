"""Python port of the EComTool Stage-2 economic model (MATLAB -> Python).

Ported incrementally, validated sheet-by-sheet against the golden MATLAB
Results. `frontend` covers the deterministic front-end (inputs, prices, load
profiles, billing-year shift, indices) shared by SC and ARB. Dispatch and
economics land in mode-specific modules on top of it.
"""
