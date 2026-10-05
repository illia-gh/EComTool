# EComTool: Techno-Economic Planning for Energy Communities

Standalone PV + BESS techno-economic tool. EVs are modeled as charging loads. Supports SC (self-consumption) 
and ARB (arbitrage) analysis through the local browser UI. Results were validated in MATLAB.

## Start

Requirements: Windows 10/11 and Python 3.12+ from python.org.

Double-click `Start.bat`. Launcher creates `.venv`, installs dependencies, starts local server,
and opens `http://127.0.0.1:8000`.

Full English user guide: [`USER-GUIDE.md`](USER-GUIDE.md).

Workflow:

1. Select SC or ARB.
2. Upload `EComTool_User_Input.xlsx`.
3. Review advanced parameters. Yellow fields affect project cost.
4. Click **Run Analysis**.
5. Review the report, which appears as soon as the calculation finishes. The Profiles and
   Results workbooks are prepared in the background; their download buttons show
   "Preparing…" until the file is ready.

Use **Clean up old files** to preview and remove old UI-generated cache files. The newest five
runs per category stay; current input, current results, source files, and unknown files stay.

Ukraine uses `price_forecast/Price_EUR_kWh_UA.xlsx`; Latvia uses
`price_forecast/Price_EUR_kWh_LV.xlsx`.

Stop using `Ctrl+C` in the launcher window or `Stop.bat`.

## Performance diagnostics

Every run writes a detailed log and JSON under `outputs/runtime/performance/`: machine,
Python/package versions, input hash, cache status, artifact sizes, and per-stage timings.
Attach the matching `.log` and `.json` when reporting a slow run. On laptops, run on mains
power: battery power-saving can make running much slower.

In the browser UI, tick **Cold Stage-1 benchmark** only to measure an uncached Stage-1 run, then
download the **performance log** and **performance JSON** from Results → Diagnostics.

## Outputs

- `outputs/report_sc.html` or `outputs/report_arb.html`
- `outputs/runtime/EComTool_Output.xlsx` (Profiles; UI background export)
- `outputs/runtime/Results_PV_BESS_SC_20y.xlsx` (SC; UI background export)
- `outputs/runtime/Results_PV_BESS_ARB_20y.xlsx` (ARB; UI background export)
- `outputs/runtime/analysis_sc.log` or `analysis_arb.log`
- `outputs/runtime/analysis_sc_timing.json` or `analysis_arb_timing.json`

Timing JSON records Stage-1, Python Stage-2, report, and total seconds, UTC timestamps,
and final status. The UI result panel shows the same duration summary.

Detailed voltage diagnostics stay in the Results workbook. HTML report shows yearly peak loading
and PCC min/max voltage; values outside 0.80–1.20 p.u. display `Voltage beyond the range`.

Green Tariff applies only to Ukraine through calendar year 2029 inclusive. Starting 2030,
the export tariff uses the required positive Own Fixed export value from `Input!B92`.
Simulation start year defaults to the current year, but not earlier than 2026.
Default rate follows NEURC Resolution No. 1028 (549.48 kop/kWh excl. VAT, solar
installations of consumers and energy cooperatives up to 150 kW).

## License

Code: [MIT](LICENSE). Bundled data: see below.

## Data sources and reuse

The MIT licence covers the EComTool code only. Bundled data come from individual providers
and remain subject to their terms: PVGIS (European Commission JRC), Renewables.ninja
(CC BY-NC 4.0, noncommercial use only), ElaadNL, ENTSO-E Transparency Platform, and
Ukraine's Market Operator. For commercial use, select PVGIS or your own PV profiles.
Details per file: [`USER-GUIDE.md`](USER-GUIDE.md#10-data-sources-and-reuse). If you
hold rights to a bundled file and want it removed, [open an issue](https://github.com/illia-gh/EComTool/issues).

## How to cite

Diahovchenko, I., Petrichenko, L., & Nozdrenkov, V. (2026). *EComTool*
(Version 2026.10.03.1) [Computer software].
[https://github.com/illia-gh/EComTool](https://github.com/illia-gh/EComTool).

Machine-readable citation metadata: [`CITATION.cff`](CITATION.cff).

## Contributors and funding

| Contributor | Roles |
|---|---|
| **Illia Diahovchenko** (Ukraine) | 🔬 Research · 🤔 Ideas · 💻 Code · 🔍 Funding · 📆 Project management |
| **Lubova Petrichenko** (Latvia) | 🔬 Research · 🤔 Ideas · 💻 Code (MATLAB model) · 🔍 Funding · 📆 Project management |
| **Valerii Nozdrenkov** | 🔬 Research · 💻 Code · 🚧 Maintenance |

Research behind development of this tool was funded by the Ministry of Education
and Science of Ukraine under grant No. 0125U002848 and by the Latvian Council of
Science for project LV_UA/2026/2, “Development of an open-source tool to support
energy communities with electric vehicles and battery energy storage.”

Release metadata: `PUBLISH-INFO.md`.
