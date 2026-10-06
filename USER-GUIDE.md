# EComTool — User Guide


EComTool models an energy community with participant demand, electric vehicles (EVs), solar PV, and battery energy storage (BESS). The published version runs locally on Windows through a Python server and browser interface. Results were validated in MATLAB.

EComTool is primarily designed for RECs with installed power up to 150 kW, but it can also support the development of larger RECs.

`inputs/EComTool_User_Input.xlsx` and the Python application define the current input contract. Keep a working copy of the workbook outside the EComTool folder so updates cannot replace your inputs.

## Contents

1. [What EComTool calculates](#1-what-ecomtool-calculates)
2. [Guide to input parameters and results](#2-guide-to-input-parameters-and-results)
3. [Quick start](#3-quick-start)
4. [Main files and folders](#4-main-files-and-folders)
5. [Fill in EComTool_User_Input.xlsx](#5-fill-in-ecomtool_user_inputxlsx)
6. [Own profiles and tariffs](#6-own-profiles-and-tariffs)
7. [Read the results](#7-read-the-results)
8. [Find saved results](#8-find-saved-results)
9. [Clean up old files](#9-clean-up-old-files)
10. [Troubleshooting](#10-troubleshooting)
11. [Data sources and reuse](#11-data-sources-and-reuse)
12. [Contributors and funding](#12-contributors-and-funding)

## 1. What EComTool calculates

Select a planning horizon of 5, 10, 12, 15, 18, or 20 years. Each model year contains 35,040 15-minute intervals (365 days).

- **SC (self-consumption):** BESS increases local use of PV energy.
- **ARB (arbitrage):** BESS charging and discharging also respond to electricity prices.

Outputs include energy flows, electricity bills, savings, investment cost (CAPEX), net present value (NPV), discounted cash flow, BESS degradation, and preliminary voltage screening at the point of common coupling (PCC).

## 2. Guide to input parameters and results

### Input File Terminology

| Parameter | Explanation |
| :--- | :--- |
| **Existing PV Installed Power, kW** | The total rated power of the solar panels already installed at the start of the project. |
| **Projected PV Installed Power, kW** | The planned total rated power of the solar panels in the future scenario. |
| **PV Profile** | Selects the source and format of the solar electricity generation time series used in the simulation. |
| **Existing EV Number** | The number of electric vehicles at the start of the project. |
| **Projected EV Number** | The planned total number of electric vehicles in the future scenario. |
| **EV Home Charging Profile** | A time series showing electricity demand for EV charging at home. |
| **EV Public Charging Profile** | A time series showing electricity demand for EV charging at public charging points. |
| **EV Workplace Charging Profile** | A time series showing electricity demand for EV charging at workplaces. |
| **EV Fast Charging Profile** | A time series showing electricity demand for EV charging at high-power charging stations. |
| **EV Home Charging, %** | The share assigned to home charging in the model’s EV charging mix. |
| **EV Public Charging, %** | The share assigned to public charging in the model’s EV charging mix. |
| **EV Workplace Charging, %** | The share assigned to workplace charging in the model’s EV charging mix. |
| **EV Fast Charging, %** | The share assigned to fast charging in the model’s EV charging mix. |
| **Smart Charging Participation, %** | The share of EVs participating in controlled charging, where charging times or power can be adjusted. |
| **Smart EV Home Charging Profile** | A time series showing electricity demand for controlled EV charging at home. |
| **Smart EV Public Charging Profile** | A time series showing electricity demand for controlled EV charging at public charging points. |
| **Existing BESS Installed Capacity, kWh** | The energy storage capacity of the stationary battery system already installed at the start of the project. |
| **Projected BESS Installed Capacity, kWh** | The planned total energy storage capacity of the stationary battery system in the future scenario. |
| **From Grid Own Fixed Tariff, €/kWh** | The user-defined fixed price paid for each kWh of electricity purchased from the grid. |
| **To Grid Own Fixed Tariff, €/kWh** | The user-defined fixed price received for each kWh of electricity exported to the grid. |
| **Age of Existing PV Installations, years** | The number of years the existing solar panels have been in operation before the simulation starts. |
| **PV Degradation, %** | The annual percentage decrease in solar panel output due to ageing. |
| **BESS Degradation, %** | The annual percentage decrease in the battery’s energy storage capacity due to ageing. |
| **Planning Horizon, years** | The number of years covered by the simulation and financial assessment. |
| **PV Adoption Trajectory** | The pattern describing how installed PV power increases from the existing level to the projected level over time. |
| **EV Adoption Trajectory** | The pattern describing how the number of EVs increases from the existing number to the projected number over time. |
| **Annual Load Growth Rate, %** | The percentage by which electricity demand increases each year. |

#### Operational Strategies

* **SC — Self-Consumption Strategy:** A strategy that prioritises using locally generated PV electricity to meet the community’s demand. The second priority is that the surplus PV electricity is stored in the battery for later use. Any remaining surplus is exported to the grid.
* **ARB — Arbitrage Strategy:** A strategy that uses electricity price differences to reduce costs or generate revenue. The battery can charge when electricity prices are low and discharge when prices are high, supplying the community or exporting electricity to the grid, aiming to maximize the economic benefit, subject to the model’s constraints.

---

### Output Results Terminology

| Term | Explanation |
| :--- | :--- |
| **NPV (Net Present Value)** | The value of the project over the selected period, expressed in today’s money. It accounts for the initial investment, future financial benefits, and the discount rate. A positive NPV means the project is financially beneficial under the chosen assumptions. |
| **Discount rate** | The rate used to convert future money into its value today. |
| **Average savings** | The average annual financial benefit of the project compared with the reference case without the project. |
| **Discounted payback** | The time needed for the project’s discounted financial benefits to recover its initial investment. |
| **CAPEX (Capital Expenditure)** | The initial cost of installing the project. Here it includes the photovoltaic system (PV) and battery energy storage system (BESS). |
| **PV generation** | The total electricity produced by the photovoltaic panels over the selected period. |
| **Self-supply** | The share of electricity demand covered by PV generation and BESS discharge. For an arbitrage strategy, BESS discharge may include electricity previously charged from the grid, so this value does not necessarily represent the share covered by renewable energy. |
| **Grid import** | The total electricity purchased from the grid over the selected period. |
| **Grid export** | The total electricity sent from the project to the grid over the selected period. |

---

### Advanced Model Parameters

The following parameters are intended for advanced users; modifying them without a deeper understanding of the tool is not recommended.

| Parameter | Explanation |
| :--- | :--- |
| **dso_UA** | Distribution system operator (DSO) network charge for electricity in Ukraine, EUR/kWh. |
| **dso_LV** | DSO network charge for electricity in Latvia, expressed, EUR/kWh. |
| **trade** | Additional electricity trading fee applied per unit of electricity, EUR/kWh. |
| **dso_patst** | Additional electricity transmission/system-related charge used in the electricity cost calculation, EUR. |
| **C_rate** | Maximum BESS charging or discharging rate relative to its energy capacity. E.g., a C-rate of 0.5 means that a 100 kWh battery can charge or discharge at up to 50 kW. |
| **DoD** | Depth of Discharge — the maximum share of the battery capacity that can be discharged. A DoD of 0.8 means that 80% of the nominal battery capacity is usable. |
| **eta_ch** | Charging efficiency of the BESS. A value of 0.95 means that 95% of the energy supplied during charging is stored in the battery. |
| **eta_dis** | Discharging efficiency of the BESS. A value of 0.95 means that 95% of the energy taken from the battery is delivered to the system. |
| **batt_cost_EUR_per_kWh** | Battery investment cost per unit of installed BESS energy capacity, EUR/kWh. |
| **bess_inv_cost_EUR** | Fixed investment cost of the BESS inverter, EUR. |
| **bess_opex_rate** | Annual BESS operating and maintenance cost expressed as a fraction of the BESS investment cost. E.g., 0.01 corresponds to 1% per year. |
| **bess_replace_year** | Project year in which the BESS is replaced. A value of 0 means that no scheduled replacement is applied. |
| **bess_replace_factor** | Cost factor applied when calculating the BESS replacement cost. |
| **pv_inverter_ratio** | Ratio used to size the PV inverter relative to the installed PV capacity. E.g., a value of 0.8 means that inverter capacity is 80% of the PV nominal capacity. |
| **pv_cost_EUR_per_kW** | PV investment cost per unit of installed PV capacity, EUR/kW. |
| **pv_installation_cost_EUR_per_kW** | PV installation cost per unit of installed PV capacity, EUR/kW. |
| **pv_inv_cost_EUR** | Fixed investment cost of the PV inverter, expressed in EUR. |
| **pv_opex_rate** | Annual PV operating and maintenance cost expressed as a fraction of the PV investment cost. A default value of 0.01 corresponds to 1% per year. |
| **pv_inv_replace_year** | Project year in which the PV inverter is replaced. A value of 0 means that no scheduled replacement is applied. |
| **pv_inv_replace_cost_EUR** | Cost of replacing the PV inverter, EUR. |
| **discount_rate** | Discount rate used to convert future cash flows to their present value. A default value of 0.07 corresponds to 7% per year. |
| **grid_emission_kg_per_kWh** | Grid electricity emission factor, expressed in kg CO₂ per kWh of electricity consumed from the grid. |
| **simulation_start_year** | Calendar year in which the simulation starts. |
| **green_tariff_end_year** | Last year in which the “Green Tariff” is applied to eligible exported electricity. |
| **green_tariff_EUR_per_kWh** | “Green Tariff” rate paid for eligible electricity exported to the grid, EUR/kWh. |
| **cycle_cost** | Cost assigned to BESS cycling to account for battery usage and discourage unnecessary charging and discharging. |
| **phase_count** | Number of electrical phases used in the grid connection model. A default value of 3 represents a three-phase system. |
| **billing_start_month** | Month in which the annual billing or accounting period starts. |
| **billing_start_day** | Day of the month on which the annual billing or accounting period starts. |
| **power_factor** | Ratio of active power to apparent power (cos φ). A default value of 0.95 represents a power factor of 0.95. |
| **V_grid_pu** | Grid voltage expressed in per-unit (p.u.) relative to the nominal voltage. A default value of 1.05 corresponds to 105% of nominal voltage. |
| **screening_years** | Selected project years used for intermediate technical or economic evaluation, e.g. Years 5, 10, 15, and 20. |
| **w_export_SC** | Weight or penalty applied to electricity exported to the grid in the Self-Consumption (SC) optimization. |
| **w_curt_SC** | Weight or penalty applied to curtailed renewable energy in the SC optimization. |
| **sc_objective** | Defines the optimization objective used by the SC dispatch strategy. energy prioritizes energy-based SC. |
| **sc_use_cost** | Determines whether electricity costs are included in the SC dispatch objective. false means that dispatch is based on energy rather than cost. |
| **fast_sc_dispatch** | Enables the simplified/accelerated SC dispatch algorithm. true uses the faster calculation method. |

## 3. Quick start

1. Copy `inputs/EComTool_User_Input.xlsx` to a working location. Fill in its `Input` sheet. Keep the other sheets and field names unchanged.
2. If you select an `Own` profile or tariff, put its file in the matching subfolder of `inputs/User Own Input/` before analysis. See [section 6](#6-own-profiles-and-tariffs).
3. Double-click `Start.bat` in the EComTool folder. First launch creates `.venv` and installs dependencies. Open `http://127.0.0.1:8000` if the browser does not open automatically.
4. Select **SC** (default) or **ARB**, select the workbook, and click **Upload Input File**.
5. Review **Advanced model parameters** if needed. Yellow fields affect CAPEX.
6. Click **Run Analysis** and wait for `Analysis complete`.
7. Read the HTML report. Download files with **Download Stage 1 Output (Profiles XLSX)**, **Download Report (HTML)**, and **Download Stage 2 Output (Results XLSX)**. The report appears first; both workbooks are prepared in the background. A button showing `Preparing Stage 1 Output…` or `Preparing Stage 2 Output…` becomes active when its file is ready. Performance log and JSON are under **Diagnostics**.
8. Double-click `Stop.bat` or press `Ctrl+C` in the server window when finished.

Leave **Cold Stage-1 benchmark** off for normal runs. Enable it only to measure an uncached Stage 1 run. ARB can be slower on a laptop running on battery power.

## 4. Main files and folders

| Path | Purpose |
|---|---|
| `inputs/EComTool_User_Input.xlsx` | Input template; edit a copy. |
| `inputs/Database/` | Bundled PV, EV, and load profiles, plus tariffs. |
| `inputs/User Own Input/` | Your own profiles and dynamic tariffs. |
| `price_forecast/Price_EUR_kWh_UA.xlsx` | Ukraine spot-price forecast. |
| `price_forecast/Price_EUR_kWh_LV.xlsx` | Latvia spot-price forecast. |
| `tool/defaults/*.json` | Default Python model parameters. |
| `outputs/runtime/` | Working spreadsheets, logs, uploaded input copies, and configuration snapshots. |
| `outputs/report_sc.html`, `outputs/report_arb.html` | Latest HTML reports. |

The browser uploads only the input workbook. Copy your own profile and tariff files to `User Own Input` manually before analysis.

## 5. Fill in `EComTool_User_Input.xlsx`

Edit the `Input` sheet. The `Geography`, `Consumers`, `Profiles`, and `Tariffs` sheets supply drop-down lists. Do not change their structure: the Python loader expects it.

### Geography — row 4

| Field | Enter |
|---|---|
| `Country` | `Ukraine` or `Latvia`. Selects bundled profiles, tariffs, and price forecast. |
| `City` | A listed city in the selected country. Required for a database PV profile. |
| `Area Type` | `Urban`, `Suburban`, or `Rural`. Selects the grid impedance at the point of common coupling (PCC) for `Grid_Screening`: Urban 0.05 + j0.02 Ω, Suburban 0.15 + j0.06 Ω, Rural 0.40 + j0.15 Ω. It does not change energy flows or economics. |
| `From Grid` | `Fixed`, `Dynamic`, `Own Fixed`, or `Own Dynamic`. |
| `To Grid` | The same choices; `Green Tariff` is also available for Ukraine. |

`Own Fixed` uses `Input!B91` for import and `Input!B92` for export. `Own Dynamic` uses a 15-minute series from `User Own Input/Tariffs/` as the final tariff in EUR/kWh, repeated without forecasting for each model year.

For Ukraine, the default `Fixed` import price is 7.50 UAH/kWh at an assumed rate of 52 UAH/EUR; it already includes distribution. `Dynamic` import adds the selected country's DSO rate to spot prices: `dso_UA = 0.0096 EUR/kWh` or `dso_LV = 0.047 EUR/kWh`, plus configured trade and other tariff terms. DSO rates can be changed in **Advanced model parameters**. For Latvian `Fixed` import, the model adds configured DSO and trade terms; it does not add them again to Ukraine's default fixed price.

`Green Tariff` is a Ukraine-only export option. Under the current defaults, the model applies `0.10567 EUR/kWh` to eligible PV-origin exports through **2029**; later years use the positive `Own Fixed` export value in `Input!B92`. The model uses whole calendar years and does not split 2026 at 1 July. The simulation start year is set when the run begins and is at least 2026. These are model assumptions, not a statement of current tariff eligibility.

The default rate is 549.48 kop/kWh excluding VAT, set by NEURC Resolution No. 1028 of 30 June 2026 (effective 1 July 2026), converted at a fixed 52 UAH/EUR. That rate covers roof- or facade-mounted solar installations of consumers, including energy cooperatives, with installed capacity up to **150 kW**, commissioned from 2025 through 2029. For Ukraine, Green Tariff results are intended for energy cooperatives within that 150 kW limit; larger installations face different market conditions. NEURC revises the rate quarterly: to use a newer rate, change `green_tariff_EUR_per_kWh` under **Advanced model parameters**.

In ARB mode, the model tracks PV-origin and grid-origin BESS energy separately. `PV → Grid` and `PV → BESS → Grid` can receive Green Tariff treatment during the configured period. `Grid → BESS → Grid` uses ordinary export price `max(spot − trade, 0) + dso_patst`. Initial or otherwise unclassified BESS energy is treated as grid-origin.

### ECom Assets — rows 10–29

| Cells | Enter |
|---|---|
| `B10`, `B11` | Existing and additional planned PV capacity, kW. |
| `B12` | PV profile source: `Database, PVGIS`, `Database, Renewables.Ninja`, or one of the `Own` formats. For commercial use, select PVGIS or your own profiles: Renewables.ninja data are for noncommercial use only. |
| `B14`, `B15` | Existing and additional planned EV counts. |
| `B16:B19` | Regular EV charging profiles: home, public, workplace, fast. |
| `B20:B23` | Shares of home, public, workplace, and fast charging, %. Together they should equal 100%. |
| `B24` | Share of EVs using smart charging, 0–100%. |
| `B25:B26` | Smart home and smart public charging profiles. |
| `B28`, `B29` | Existing and additional planned nominal BESS energy capacity, kWh. |

EComTool does not set Renewables.ninja as an application default. The input workbook can retain a saved choice in `Input!B12`; check that choice before each run.

Enter **BESS energy capacity in kWh**, not power. The model calculates charge/discharge power as `C_rate × capacity_kWh`; for example, `0.5 × 120 kWh = 60 kW`. A CAPEX rate such as `650 EUR/kWh` applies to the entered capacity.

`Projected` means an *additional* amount. For example, 10 kW existing PV plus 90 kW projected PV gives 100 kW after full adoption.

### ECom Participants — rows 35–84

Each filled row represents one participant group. A row without `Type` is ignored.

| Column | Enter |
|---|---|
| `A — ECom Participant ID` | Unique ID; determines filename for an own load profile. |
| `B — Category` | Residential, Commercial, Educational, or Industrial; controls the `Type` list. |
| `C — Type` | Consumer type. This field activates the row. |
| `D — Number` | Count of identical participants; blank means 1. |
| `E — Annual consumption, kWh` | Optional. If given, the load shape is scaled to this annual consumption multiplied by `Number`. |
| `F — Load Profile` | `Database`, `Own, standard format, 15 min`, or `Own, standard format, 1 hour`. |

Note: if a participant's own profile is selected in column F, the columns `Category`, `Type`, and `Annual consumption, kWh` will not affect the simulation results and might be left blank.

### Other Settings — rows 91–99

| Cell | Enter |
|---|---|
| `B91` | Own Fixed import tariff, EUR/kWh. |
| `B92` | Own Fixed export tariff, EUR/kWh; also Green Tariff fallback after its configured end year. Must be finite and greater than zero when Green Tariff is selected. |
| `B93` | Age of existing PV, years. |
| `B94` | PV degradation, % per year. |
| `B95` | BESS degradation, % per year. Sole Stage 2 source for this value; `0 ≤ value < 100`. |
| `B96` | Planning horizon: 5, 10, 12, 15, 18, or 20 years. |
| `B97`, `B98` | PV and EV adoption: `Instantaneous`, `Linear`, or `Exponential`. New BESS capacity has no adoption cell: it is installed in full in Year 1. |
| `B99` | Annual load growth, %; blank means 0. |

## 6. Own profiles and tariffs

### General format

- Use 365 days per year, without 29 February.
- A 15-minute profile has one header row and **35,040 numeric values**.
- An hourly profile has one header row and **8,760 numeric values**. The loader repeats each hourly value four times; it does not interpolate.
- Order values from start to end of one calendar year without gaps.
- In standard CSV files, put values in column A and use a decimal point.
- Do not leave blank, text, `NaN`, or infinite values in data rows.
- Folder and file names below are part of the input contract.

### PV profile

Folder: `inputs/User Own Input/PV Profiles/`.

| `Input!B12` choice | Filename and layout |
|---|---|
| `Own, standard format, 15 min` | `own_PV_profile_15_min.csv`: header and 35,040 values in column A. |
| `Own, standard format, 1 hour` | `own_PV_profile_1_hour.csv`: header and 8,760 values in column A. |
| `Own, PVGIS` | Filename must contain `own_pvgis`. Native PVGIS layout: 10 metadata rows, a header, and 8,760 values in column B. The loader converts W to kW. |
| `Own, Renewables.Ninja` | Filename must contain `own_ninja`. Native layout: 3 metadata rows, a header, and 8,760 values in column C. |

The model scales the PV series by installed PV capacity. A standard profile should represent generation from **1 kW** of installed PV. Divide a measured profile from a larger installation by its reference capacity first.

### EV charging profiles

Folder: `inputs/User Own Input/EV Charging Profiles/`.

Use a header and 35,040 or 8,760 column-A values in kW **per EV**. The suffix matches the resolution selected in the workbook.
EComTool converts EV power to energy for each 15-minute calculation step. Keep input values in kW; do not divide them by four.

| Charging choice | Required filename stem |
|---|---|
| Regular home | `own_charging_profile_regular_home` |
| Regular public | `own_charging_profile_regular_public` |
| Regular workplace | `own_charging_profile_regular_workplace` |
| Regular fast | `own_charging_profile_regular_fast` |
| Smart home | `own_charging_profile_smart_home` |
| Smart public | `own_charging_profile_smart_public` |

Add `_15_min.csv` or `_1_hour.csv`, for example `own_charging_profile_regular_home_15_min.csv`.

### Participant load profiles

Folder: `inputs/User Own Input/Load Profiles/`.

The filename uses the participant ID in lower case with spaces replaced by underscores:

- `Participant 1`, 15 minutes → `own_participant_1_15_min.csv`.
- `Participant 6`, 1 hour → `own_participant_6_1_hour.csv`.

Use a header and 35,040 or 8,760 column-A values of power demand in kW. If `Annual consumption` is entered, the loader uses the profile as a shape and scales it. Otherwise, it multiplies the values only by `Number`.

A missing own participant file may currently produce a zero profile without stopping Stage 1. After each run, check `Load_Profiles` and total annual load.

### Own dynamic tariffs

Folder: `inputs/User Own Input/Tariffs/`.

- Import: `own_dynamic_from.xlsx`.
- Export: `own_dynamic_to.xlsx`.

Each workbook needs a header in `A1` and **35,040 values in `A2:A35041`**, in EUR/kWh. Other columns are ignored. Missing, short, malformed, nonnumeric, `NaN`, or infinite data stop the calculation. Values are used as supplied, without unit conversion or DSO/trade adders.

The bundled `own_dynamic_*.xlsx` samples are an example Ukrainian net-billing series in EUR/kWh. Replace them with your own tariff data.

### Replace a spot-price forecast

The browser has no separate upload for the spot-price forecast. Python selects `price_forecast/Price_EUR_kWh_UA.xlsx` for Ukraine or `price_forecast/Price_EUR_kWh_LV.xlsx` for Latvia.

An operator can replace the selected file. Required layout: **no header**, 35,040 rows, one column per model year, **EUR/kWh**. Python reads the first `planning horizon` columns without unit conversion. Check units and values before a run. Keep a backup outside EComTool because an update can replace bundled files.

## 7. Read the results

Start with the HTML report. Download the Results workbook for detailed checks or your own analysis.

| Sheet | Contents |
|---|---|
| `Summary` | NPV, average savings, energy totals, CAPEX, efficiency rates, and peak grid values. |
| `Energy_Balance` | Annual load, PV, grid import/export, BESS charging/discharging, curtailment, and rates. |
| `Load_Profiles` | Consumer demand separated from existing and project EV demand. |
| `SC_Flows` / `ARB_Flows` | Annual routing between PV, grid, load, and BESS. ARB separates PV-origin and grid-origin BESS flows. |
| `Economics` | Annual bills, savings, EV load cost, export credit, and cash flow. |
| `Price_Annual` | Annual average, minimum, and maximum buy and sell prices. |
| `Price_TimeSeries` | All 15-minute buy and sell prices; this sheet can be large. |
| `BESS_Degradation` | Nominal and effective capacity, power, and degradation loss. |
| `Cashflow_Timeline` | Year 0 CAPEX, annual cash flow, discounting, and cumulative NPV. |
| `Grid_Screening` | Peak import/export and approximate PCC voltage screening. |

Key interpretations:

- **NPV > 0** means the project creates discounted value under the entered assumptions; **NPV < 0** means it does not.
- **PV+BESS savings at the same project load** compares new assets with existing assets serving the *same* project load. Use this value to isolate the new PV/BESS effect.
- **EV load growth cost** is added electricity cost from more EVs, not EV purchase cost.
- **Combined bill change** includes both the PV/BESS effect and EV load growth; it can be negative despite positive PV/BESS savings.
- **Discounted payback** is the first year when cumulative discounted cash flow reaches zero. If it never does within the horizon, payback exceeds the horizon.
- **Self-consumption** is the share of PV generation used locally by load or BESS. **Self-sufficiency / load self-supply** is the share of load covered without grid import.
- `Maximum_decomposition_residual_EUR` should be close to zero; a large value signals an internal inconsistency.
- Economic results are **before profit tax**. Unused export credit carries between months of one billing year, then expires without payout.

`Grid_Screening` is preliminary voltage screening. Its grid impedance comes from `Input!C4` Area Type; `Grid_connection_type` in Summary shows the value used. It is not a power-flow or protection study and does not grant DSO connection approval.

**Download Stage 1 Output (Profiles XLSX)** exports intermediate `EComTool_Output.xlsx`, not the final economic result. The `Other Input Data` and `Run Metadata` columns are hidden but retained for compatibility.

## 8. Find saved results

| Result | Path |
|---|---|
| Profiles | `outputs/runtime/EComTool_Output.xlsx` |
| SC Results | `outputs/runtime/Results_PV_BESS_SC_20y.xlsx` |
| ARB Results | `outputs/runtime/Results_PV_BESS_ARB_20y.xlsx` |
| SC report | `outputs/report_sc.html` |
| ARB report | `outputs/report_arb.html` |
| Latest logs and timing | `outputs/runtime/analysis_*` |

HTML reports are written by every run. XLSX files are prepared in the background after the report appears.

Results filenames retain `_20y` for compatibility; the actual horizon comes from `Input!B96`. Stage 2 download names include `_sc` or `_arb` so modes do not overwrite each other in Downloads.

## 9. Clean up old files

Each upload creates a copy under `outputs/runtime/uploads/`; each UI run creates a configuration snapshot under `outputs/runtime/configs/` and archived diagnostics under `outputs/runtime/performance/`. In-memory cache holds at most two results, but archived files have no automatic disk limit.

Click **Clean up old files** in the Input workbook panel. EComTool previews file count and size, then asks for confirmation. It keeps the five newest runs per category and does not touch current input, latest Results and reports, source data, unknown files, or `.tmp` files. Cleanup cannot run during analysis. Locked files are skipped and counted. It cleans UI-generated archives only.

For manual cleanup, stop the server first. Remove old files from `outputs/runtime/uploads/`, `outputs/runtime/configs/`, and matched `.log`/`.json` pairs from `outputs/runtime/performance/`, keeping the newest files needed for troubleshooting. Restart with `Start.bat` afterward.

If every upload copy is removed, EComTool falls back to the bundled input template. Keep the latest uploaded workbook or upload it again after cleanup. Do not delete `inputs/Database/`, `inputs/User Own Input/`, `price_forecast/`, or `tool/defaults/`: these contain inputs and settings, not cache.

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| `Expected EComTool_User_Input.xlsx template...` | A Results/Profiles workbook was selected, required sheets were renamed, or template layout changed. |
| `Input!A4 must contain Ukraine or Latvia` | Select the exact country from the drop-down list. |
| `CRITICAL ERROR: PV file ... not found` | Check `Input!B12`, folder, and exact filename. |
| `CRITICAL ERROR: EV file ... not found` | Check exact EV filename and selected resolution. |
| Suspiciously low demand | A missing own participant file may silently produce a zero profile. |
| `Tariff file is missing` or `must contain exactly 35040 values` | Repair the Own Dynamic workbook; there is no zero-price fallback. |
| `country price forecast is missing` | Check the `_UA.xlsx` or `_LV.xlsx` file under `price_forecast/`. |
| Port 8000 is occupied | Run `Stop.bat`, then `Start.bat`. |
| Analysis failed | Read the UI log or `outputs/runtime/analysis_<mode>.log`. |

For a bug report, provide mode, steps to reproduce, log, and input workbook after removing private data. Do not post private participant profiles or tariffs in a public issue.

## 11. Data sources and reuse

The MIT licence covers the EComTool code only. Bundled data come from the providers listed below and remain subject to their terms; check them before redistributing a profile or price series. For commercial use, select PVGIS or your own PV profiles: Renewables.ninja profiles are for noncommercial use only. If you hold rights to a bundled file and want it removed, open an issue in this repository.

| Bundled data | Source |
|---|---|
| Database PV profiles without `ninja` in the filename, and sample `own_PVGIS.csv` | Generated with [European Commission JRC PVGIS](https://joint-research-centre.ec.europa.eu/photovoltaic-geographical-information-system-pvgis/general-information/usage-conditions-data-protection_en). Credit PVGIS/JRC. |
| Database PV profiles with `ninja` in the filename, and sample `own_ninja.csv` | From [Renewables.ninja](https://www.renewables.ninja/downloads), licensed **CC BY-NC 4.0**: noncommercial use with attribution only. Contact Renewables.ninja for commercial use. This restriction applies to these profiles only, not to EComTool code or other data. |
| `NL_charging_profile_*.csv` | Generated with the [ElaadNL charging profile generator](https://charging.elaad.nl/). |
| `standard_load_profiles_database.xlsx` | Standard load-profile database supplied by the EComTool project team. |
| Latvia price forecast and dynamic-tariff files | Derived from day-ahead prices on the [ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) (not taken directly from Nord Pool). |
| Ukraine price forecast and dynamic-tariff files | Derived from day-ahead prices published by [Ukraine's Market Operator](https://www.oree.com.ua/index.php/pricectr). |
| Other `User Own Input` samples | Format examples only. Constant-value CSV samples are synthetic; `own_dynamic_*.xlsx` is an example Ukrainian net-billing series (see [Own dynamic tariffs](#own-dynamic-tariffs)). |

## 12. Contributors and funding

EComTool contributors: Illia Diahovchenko, Lubova Petrichenko, and Valerii Nozdrenkov.

Research behind development of this tool was funded by the Ministry of Education and Science of Ukraine under grant No. 0125U002848 and by the Latvian Council of Science for project LV_UA/2026/2, “Development of an open-source tool to support energy communities with electric vehicles and battery energy storage.”
