# Levisol Supply Chain Planner

Decision-support system for Levisol's monthly production, distribution and
inventory planning (case competition build). A non-technical planner uploads
the data workbook, adjusts levers, clicks **Run Plan**, and gets a costed,
constraint-respecting plan with downloadable Excel reports.

## What it does
1. **Inventory norms** — safety stock, reorder point and days of cover for all
   957 SKU×CFA combinations and 151 SKU×Hub positions, from demand variability,
   forecast error and lead-time variability (`SS = z·√(L·σd² + d²·σL²)`).
2. **Production & distribution plan** — cost-minimising plan for the coming
   month: what to produce at each plant (25 kL batches), how to route
   plant→hub→CFA, what hub buffer to hold, and what (if anything) to
   under-serve, with the penalty cost of every such call made explicit.
3. **Scenario engine** — demand / capacity / transport-cost multipliers,
   editable capacity table, CFA safety-stock rebuild toggle, baseline
   comparison. Judges' modified input file drops straight into the uploader.

## Architecture
```
levisol_planner/
├── app.py                 # Streamlit front end (UI, scenario levers, downloads)
├── config.py              # central parameters
├── requirements.txt
├── core/
│   ├── data_loader.py     # reads the case workbook, validates, cleans
│   ├── analytics.py       # tiers, ABC-XYZ, forecast errors, inventory norms
│   ├── optimizer.py       # min-cost allocation engine (pure Python, exact
│   │                      #   transportation LP per filling line + batch logic)
│   └── reporting.py       # formatted Excel outputs
└── tests/test_engine.py   # 10 integrity checks (capacity, batches, balance,
                           #   cost reconciliation, contractual protection)
```
**No external solver required** — the optimizer implements successive-
shortest-path min-cost flow natively, so the tool runs on any laptop with
Python. This was a deliberate robustness choice for live demos.

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```
Then upload `Supply Chain Supporting Data.xlsx` (or the modified input set).

## Run the integrity tests
```bash
python -m tests.test_engine path/to/data.xlsx
```

## Deploy
- **Streamlit Cloud**: push this folder to GitHub → share.streamlit.io → select repo → `app.py`.
- **Docker**: `FROM python:3.11-slim`, `pip install -r requirements.txt`,
  `CMD streamlit run app.py --server.port 8501 --server.address 0.0.0.0`.

## Key modelling choices (defend these in Q&A)
- Safety stock is sized on **forecast-error σ**, not raw sales σ — replenishment
  is planned on the forecast, so the buffer must absorb forecast error.
- Tiering follows Exhibit F volume slabs (A=top 50% of volume, B=next 30%,
  C=15%, D=5%), z = 2.05 / 1.88 / 1.41 / 1.41.
- Hub stock above its safety-stock target is treated as free supply (production
  cost is sunk) and used before fresh production.
- If a filling line is nationally short, the lowest-penalty units are shed
  first; contractual SKUs are never shed.
- Batch rounding is economic: a partial 25 kL batch is produced only when the
  penalty avoided exceeds the cost of the batch; excess is parked at the
  cheapest hub as extra buffer.

---

# Version 2 — Enterprise Planner UI

V2 preserves the V1 analytical engine byte-for-byte (verified: baseline plan
₹9.87 cr, identical production/routing) and upgrades the experience to a
commercial-planning-tool standard. New modules only; no engine changes except
one essential determinism fix (stable SKU ordering in batch rounding, so
identical inputs always produce identical plans across runs).

## What's new
| Area | Feature |
|---|---|
| **Input Manager** | Edit demand, capacities, production & transport costs, opening inventory, penalties/contractual flags, service levels and hub SS targets **inside the app** (Save / Discard / Reset to Uploaded / Reset levers). The workbook is only the starting dataset. |
| **Scenario Manager** | 5 slots (Baseline + 4). Each stores inputs, outputs, cost, fill, inventory. Compare any two side-by-side with deltas. |
| **One-click scenarios** | Demand ±10%, Mumbai shutdown, Fuel +20%, Capacity −15%, Transport inflation, Restore baseline. |
| **Executive dashboard** | 10 KPI cards (total/production/transport/penalty cost, fill, served, hub SS compliance, utilization, inventory, contractual service) + pie, grouped bars, ABC-XYZ matrix, safety-stock heatmap. |
| **Network views** | Interactive Plant→Hub→CFA **Sankey** and an **India map** with flow-weighted lanes (plotly; graceful fallback to tables). |
| **Planner recommendations** | Auto-generated cards: bottleneck lines with cheapest relief, idle swing capacity, hub buffer status, cost-mix diagnosis, hub-stock reuse, contractual status. |
| **Optimization Summary** | Per-constraint compliance (capacity, batches, balance, contractual, hub SS, demand accounting) with reason / impact / action when not green. Soft constraints shown as ⚠️ Deferred, not ❌. |
| **Shortage explanations** | Every unmet kL gets an auto-written why / impact / suggested action (line exhausted vs uneconomic batch vs deferred buffer). |
| **Data validation** | Negative demand/costs/inventory, missing or duplicate SKUs, impossible capacities, missing lead times/penalties — friendly warnings, never a crash; errors block the run. |
| **Tooltips & glossary** | Every advanced parameter explained in plain business language; sidebar glossary. |
| **Sensitivity analysis** | Sweep demand / capacity / transport / penalty / service level; charts of cost, fill, penalty and inventory vs the lever. |
| **Exports** | + Executive Summary workbook (KPIs, recommendations, constraints) and Scenario Comparison report. |

## Updated architecture
```
core/
├── data_loader.py   (V1, unchanged)
├── analytics.py     (V1, unchanged)
├── optimizer.py     (V1 + determinism fix only)
├── reporting.py     (V1 + two additive export functions)
├── validation.py    (V2, new)  input QA — friendly errors/warnings
├── insights.py      (V2, new)  recommendations · constraint summary · shortage explanations
└── scenario.py      (V2, new)  in-app edits → CaseData · quick scenarios · sensitivity sweeps
tests/
├── test_engine.py   (V1 10-point integrity suite — still passes)
└── test_v2.py       (V2 smoke suite: 11 checks incl. V1 output parity)
app.py               (V2 UI: 7-step workflow, 11 tabs)
```

## 7-step planner workflow (no manual needed)
Upload workbook → Review inputs → Edit inputs → Run Plan → Review dashboard →
Review Optimization Summary / Shortages → Download reports. The sidebar
stepper tracks progress; traffic-light banners state plan health at a glance.
