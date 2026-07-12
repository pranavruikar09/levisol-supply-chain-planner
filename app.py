"""
Levisol Supply Chain Planner — V2 (enterprise UI)
Run:  streamlit run app.py

V2 keeps the V1 analytical engine untouched and upgrades the planner
experience: in-app input editing (no Excel round-trips), a 5-slot scenario
manager, an executive dashboard, Sankey/map network views, auto-generated
recommendations & shortage explanations, sensitivity analysis and richer
exports. A first-time planner should never need a manual.
"""
import io
import copy
import numpy as np
import pandas as pd
import streamlit as st

from core.data_loader import load_case_data, LINE_GROUPS, LINE_LABELS, PLANTS, HUBS
from core.scenario import (run_scenario, apply_overrides, sensitivity_sweep,
                           SCENARIO_SLOTS, QUICK_SCENARIOS)
from core.validation import validate_case_data
from core.insights import recommendations, constraint_summary, shortage_explanations
from core.reporting import (norms_workbook, plan_workbook,
                            exec_summary_workbook, scenario_comparison_workbook)

try:
    import plotly.graph_objects as go
    PLOTLY = True
except Exception:
    PLOTLY = False

st.set_page_config(page_title="Levisol Supply Chain Planner", layout="wide", page_icon="🛢️")
st.markdown("""
<style>
.block-container {padding-top: 4rem;}
div[data-testid="stMetric"] {background: #F4F7FB; border: 1px solid #DCE6F1;
  border-radius: 10px; padding: 10px 14px;}
div[data-testid="stMetricValue"] {font-size: 1.35rem; color: #1F4E79;}
div[data-testid="stMetricLabel"] {font-size: 0.78rem;}
.rec-card {border-left: 5px solid #1F4E79; background: #F4F7FB; border-radius: 6px;
  padding: 10px 14px; margin-bottom: 8px;}
.rec-good {border-left-color: #2E7D32;} .rec-warn {border-left-color: #ED6C02;}
.rec-error {border-left-color: #C62828;}
.step-done {color:#2E7D32;} .step-todo {color:#9E9E9E;}
</style>""", unsafe_allow_html=True)

TT = {  # tooltip glossary — plain business language
 "safety": "Extra stock held to survive demand surprises and late deliveries. Bigger buffer = fewer stock-outs but more cash tied up.",
 "fe": "How wrong last months' forecasts were. We size buffers on this error — a better forecast automatically means less inventory.",
 "penalty": "The business cost of NOT supplying 1 kL: lost margin plus lost customer loyalty (and contract damages for key accounts).",
 "fill": "Share of customer demand actually supplied. 98% target on A-tier products.",
 "batch": "Plants produce in fixed 25 kL runs. A tiny leftover need is only produced if it's worth a whole batch — otherwise the tool tells you.",
 "lt": "Days from placing a production order until stock is sellable at the CFA (production + trucking legs).",
 "sl": "The stock-out protection level a buffer is designed for. 98% means being out of stock in only 2 months out of 100.",
 "hubss": "Minimum stock each mother hub must hold at month-end to absorb supply shocks before they hit CFAs.",
 "shortcost": "Soft ₹/kL charge if a hub ends below its buffer target. Keep it below SKU penalties so real sales always win.",
 "cfass": "OFF = serve the forecast only (releases cash, CSCO mandate). ON = also produce to top CFA buffers back up.",
}

ss = st.session_state
ss.setdefault("base_data", None)
ss.setdefault("edits", {})           # table overrides from Input Manager
ss.setdefault("levers", {})          # multipliers / shutdown from quick scenarios
ss.setdefault("scenarios", {})       # slot -> run_scenario result
ss.setdefault("active", "Baseline")
ss.setdefault("settings", {"include_cfa_ss": False, "hub_short_cost": 75000})

# ================================================================== SIDEBAR
with st.sidebar:
    st.title("🛢️ Levisol Planner")
    # --- workflow stepper
    steps = [("Upload workbook", ss.base_data is not None),
             ("Review / edit inputs", bool(ss.edits) or ss.base_data is not None),
             ("Run optimization", ss.active in ss.scenarios),
             ("Review dashboard & constraints", ss.active in ss.scenarios),
             ("Download reports", ss.active in ss.scenarios)]
    for i, (name, done) in enumerate(steps, 1):
        st.markdown(f"<span class='{'step-done' if done else 'step-todo'}'>"
                    f"{'✔' if done else i} &nbsp;{name}</span>", unsafe_allow_html=True)
    st.divider()

    up = st.file_uploader("Monthly data workbook (.xlsx)", type=["xlsx"],
        help="Same layout as 'Supply Chain Supporting Data.xlsx'. This is the STARTING dataset — all further editing happens inside the app.")
    if up is not None and ss.get("_upname") != up.name + str(up.size):
        ss.base_data = load_case_data(io.BytesIO(up.getvalue()))
        ss._upname = up.name + str(up.size)
        ss.edits, ss.levers, ss.scenarios = {}, {}, {}
        ss.active = "Baseline"

    st.divider()
    st.subheader("Scenario slot")
    ss.active = st.radio("Working scenario", SCENARIO_SLOTS, index=SCENARIO_SLOTS.index(ss.active),
        help="Each slot stores its own inputs and results. Baseline should stay untouched as your reference.",
        label_visibility="collapsed")

    st.subheader("One-click scenarios")
    qcols = st.columns(2)
    for i, (name, lev) in enumerate(QUICK_SCENARIOS.items()):
        if qcols[i % 2].button(name, use_container_width=True, key=f"q_{name}"):
            ss.levers = dict(lev)
    if st.button("Restore Baseline levers", use_container_width=True):
        ss.levers = {}
    if ss.levers:
        st.caption("Active levers: " + ", ".join(f"{k}={v}" for k, v in ss.levers.items()))

    with st.expander("⚙️ Advanced settings"):
        ss.settings["include_cfa_ss"] = st.toggle("Rebuild CFA safety stock", ss.settings["include_cfa_ss"], help=TT["cfass"])
        ss.settings["hub_short_cost"] = st.number_input("Hub SS shortfall cost (₹/kL)", 0, 500000,
            ss.settings["hub_short_cost"], 5000, help=TT["shortcost"])
    with st.expander("📖 Glossary (plain language)"):
        for k, label in [("safety", "Safety stock"), ("fe", "Forecast error"), ("penalty", "Penalty cost"),
                         ("fill", "Fill rate"), ("batch", "25 kL batch rule"), ("lt", "Lead time"),
                         ("sl", "Service level"), ("hubss", "Hub safety stock")]:
            st.markdown(f"**{label}** — {TT[k]}")

    run_clicked = st.button("▶ Run Plan", type="primary", use_container_width=True,
        disabled=ss.base_data is None,
        help="Applies your edits and levers to this scenario slot and re-optimizes (≈5-10 s).")

if ss.base_data is None:
    st.title("📦 Levisol Supply Chain Planner")

    st.caption(
        "Upload the monthly planning workbook using the left sidebar. "
        "Once uploaded, all exhibits are validated automatically. "
        "You can then edit inputs, run scenarios, compare plans, and download reports without reopening Excel."
    )
    st.stop()

issues = validate_case_data(apply_overrides(ss.base_data, {**ss.edits, **ss.levers}))
blocking = [i for i in issues if i["level"] == "error"]

if run_clicked or ss.pop("_run_requested", False):
    if blocking:
        st.error("Fix the blocking input errors (see Data Quality tab) before running:\n" +
                 "\n".join("• " + b["message"] for b in blocking))
    else:
        with st.spinner("Optimizing… norms → allocation → batching"):
            ss.scenarios[ss.active] = run_scenario(
                ss.base_data, {**ss.edits, **ss.levers},
                include_cfa_ss=ss.settings["include_cfa_ss"],
                hub_short_cost=ss.settings["hub_short_cost"],
                hub_ss_override=ss.get("hub_ss_override"))

res = ss.scenarios.get(ss.active)

# ================================================================== HEADER / BANNER
if res:
    plan = res["plan"]
    unmet_kl = plan.unmet["kl"].sum() if len(plan.unmet) else 0.0
    contr_bad = (len(plan.unmet) and plan.unmet[~plan.unmet["cfa"].str.contains("hub SS", na=False)]
                 ["contractual"].fillna(False).any())
    if contr_bad:
        st.error(f"🔴 **{ss.active}** — CONTRACTUAL SKU UNDER-SERVED. See Shortages tab before publishing.")
    elif unmet_kl > 0.5 or plan.hub_position["shortfall_kl"].sum() > 0.5:
        st.warning(f"🟡 **{ss.active}** — feasible with trade-offs: {unmet_kl:,.1f} kL unmet, "
                   f"{plan.hub_position['shortfall_kl'].sum():,.1f} kL hub buffer deferred.")
    else:
        st.success(f"🟢 **{ss.active}** — all demand served, all hub safety stocks met.")
else:
    st.info(f"**{ss.active}** has not been run yet — review inputs, then press **Run Plan**.")

TAB_NAMES = ["🏠 Dashboard", "📝 Input Manager", "🎛 Scenarios", "📦 Norms", "🏭 Production",
             "🚚 Network & Map", "🧭 Optimization Summary", "⚠️ Shortages",
             "📈 Sensitivity", "🧪 Data Quality", "⬇ Downloads"]
tabs = st.tabs(TAB_NAMES)

# ================================================================== DASHBOARD
with tabs[0]:
    if not res:
        st.caption("Run the plan to populate the dashboard.")
    else:
        k, c = plan.kpis, plan.costs
        freight = c["transport_plant_hub"] + c["transport_hub_cfa"]
        util_used = [v for kk, v in k["utilization"].items()]
        hubok = 100 * (1 - plan.hub_position["shortfall_kl"].sum() /
                       max(plan.hub_position["ss_target_kl"].sum(), 1e-9))
        contr_dem = res["data"].jan_forecast.merge(
            res["data"].sku[res["data"].sku.contractual].reset_index()[["sku"]], on="sku")["qty"].sum()
        contr_un = (plan.unmet[plan.unmet["contractual"].fillna(False) &
                    ~plan.unmet["cfa"].str.contains("hub SS", na=False)]["kl"].sum()
                    if len(plan.unmet) else 0.0)
        r1 = st.columns(5)
        r1[0].metric("Total cost", f"₹{c['total']/1e7:,.2f} cr")
        r1[1].metric("Production cost", f"₹{c['production']/1e7:,.2f} cr")
        r1[2].metric("Transport cost", f"₹{freight/1e7:,.2f} cr")
        r1[3].metric("Penalty cost", f"₹{(c['penalty_unmet']+c['hub_ss_shortfall'])/1e7:,.3f} cr", help=TT["penalty"])
        r1[4].metric("Fill rate", f"{k['fill_rate']*100:,.2f}%", help=TT["fill"])
        r2 = st.columns(5)
        r2[0].metric("Demand served", f"{k['served_kl']:,.0f} kL", f"of {k['total_demand_kl']:,.0f} kL", delta_color="off")
        r2[1].metric("Hub SS compliance", f"{hubok:,.1f}%", help=TT["hubss"])
        r2[2].metric("Avg line utilization", f"{np.mean(util_used)*100:,.0f}%")
        r2[3].metric("Inventory norms", f"{res['summary']['inventory_norm_kl']:,.0f} kL", "network safety stock", delta_color="off", help=TT["safety"])
        r2[4].metric("Contractual service", f"{100*(1-contr_un/max(contr_dem,1e-9)):,.1f}%")

        st.divider()
        a, b = st.columns(2)
        with a:
            st.subheader("Cost breakdown")
            cd = {"Production": c["production"], "Freight plant→hub": c["transport_plant_hub"],
                  "Freight hub→CFA": c["transport_hub_cfa"],
                  "Penalties": c["penalty_unmet"] + c["hub_ss_shortfall"]}
            if PLOTLY:
                fig = go.Figure(go.Pie(labels=list(cd), values=[v/1e7 for v in cd.values()], hole=0.45,
                                       marker=dict(colors=["#1F4E79", "#5B9BD5", "#9DC3E6", "#ED6C02"])))
                fig.update_layout(height=320, margin=dict(t=10, b=10, l=10, r=10))
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.bar_chart(pd.Series({kk: v/1e7 for kk, v in cd.items()}))
        with b:
            st.subheader("Plant × line utilization")
            u = pd.DataFrame([{"plant": kk.split("|")[0], "line": LINE_LABELS.get(kk.split("|")[1]),
                               "util": v*100} for kk, v in k["utilization"].items()])
            if PLOTLY:
                fig = go.Figure()
                for p in PLANTS:
                    dd = u[u.plant == p]
                    fig.add_bar(name=p, x=dd["line"], y=dd["util"])
                fig.add_hline(y=100, line_dash="dot", line_color="red")
                fig.update_layout(barmode="group", height=320, yaxis_title="%",
                                  margin=dict(t=10, b=10, l=10, r=10))
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.dataframe(u.pivot(index="line", columns="plant", values="util").round(0))

        a, b = st.columns(2)
        with a:
            st.subheader("Demand vs served by tier")
            jf = res["data"].jan_forecast.assign(tier=lambda x: x.sku.map(res["tiers"]["tier"]))
            dem_t = jf.groupby("tier")["qty"].sum()
            un_t = (plan.unmet[~plan.unmet["cfa"].str.contains("hub SS", na=False)]
                    .assign(tier=lambda x: x.sku.map(res["tiers"]["tier"])).groupby("tier")["kl"].sum()
                    if len(plan.unmet) else pd.Series(dtype=float))
            tt_df = pd.DataFrame({"Demand": dem_t, "Served": dem_t - un_t.reindex(dem_t.index).fillna(0)})
            if PLOTLY:
                fig = go.Figure()
                fig.add_bar(name="Demand", x=tt_df.index, y=tt_df["Demand"], marker_color="#9DC3E6")
                fig.add_bar(name="Served", x=tt_df.index, y=tt_df["Served"], marker_color="#1F4E79")
                fig.update_layout(barmode="group", height=300, yaxis_title="kL", margin=dict(t=10, b=10))
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.bar_chart(tt_df)
        with b:
            st.subheader("ABC-XYZ portfolio mix")
            t = res["tiers"].copy(); t["xyz"] = t.index.map(res["xyz"])
            ct = pd.crosstab(t["tier"], t["xyz"])
            #st.dataframe(ct.style.background_gradient(cmap="Blues"), use_container_width=True)
            st.dataframe(ct)
            st.caption("A-X = predictable flagships (lean buffers work) · D-Z = erratic tail (watch list). "
                       + TT["sl"])

        st.subheader("Safety-stock heatmap (kL) — tier × CFA")
        hm = res["norms"].assign(tier=lambda x: x.sku.map(res["tiers"]["tier"])) \
            .pivot_table(index="tier", columns="cfa", values="safety_stock_kl", aggfunc="sum").round(0)
        hm.columns = [c.replace(" CFA", "") for c in hm.columns]
        # st.dataframe(hm.style.background_gradient(cmap="YlOrRd", axis=None), use_container_width=True)
        st.dataframe(hm)

        st.subheader("📋 Planner recommendations")
        for r in recommendations(plan, res["data"], res["tiers"]):
            cls = {"good": "rec-good", "warn": "rec-warn", "error": "rec-error"}.get(r["kind"], "")
            st.markdown(f"<div class='rec-card {cls}'><b>{r['icon']} {r['title']}</b><br>"
                        f"<span style='font-size:0.88rem'>{r['detail']}</span></div>",
                        unsafe_allow_html=True)

# ================================================================== INPUT MANAGER
with tabs[1]:
    st.subheader("📝 Input Manager")
    st.caption("The uploaded workbook is only the starting point — edit any input here, save, and re-run. "
               "No Excel round-trips needed for monthly planning.")
    base_view = apply_overrides(ss.base_data, ss.edits)   # current saved state
    etabs = st.tabs(["Demand", "Plants & capacity", "Transport costs", "Opening inventory",
                     "SKU penalties", "Service levels", "Hub SS targets"])
    pending = {}

    with etabs[0]:
        st.caption("January demand per SKU × CFA (kL). Filter, edit cells, then Save.")
        fsku = st.multiselect("Filter SKUs", sorted(base_view.jan_forecast.sku.unique()), key="im_sku")
        dview = base_view.jan_forecast[["sku", "cfa", "qty"]]
        if fsku:
            dview = dview[dview.sku.isin(fsku)]
        edited = st.data_editor(dview, key="ed_dem", use_container_width=True, height=360,
                                disabled=["sku", "cfa"], column_config={
                                    "qty": st.column_config.NumberColumn("Demand (kL)", min_value=0.0, format="%.2f")})
        full = base_view.jan_forecast[["sku", "cfa", "qty"]].set_index(["sku", "cfa"])
        full.update(edited.set_index(["sku", "cfa"]))
        pending["demand"] = full.reset_index()
    with etabs[1]:
        st.caption("Line capacities (kL/month) and production cost (₹/kL) by plant.")
        pv = base_view.plants[LINE_GROUPS + ["prod_cost"]].rename(columns=LINE_LABELS)
        ep = st.data_editor(pv, key="ed_pl", use_container_width=True)
        pending["plants"] = ep.rename(columns={v: k for k, v in LINE_LABELS.items()})
    with etabs[2]:
        c1, c2 = st.columns(2)
        with c1:
            st.caption("Plant → Hub (₹/kL)")
            pending["plant_hub_cost"] = st.data_editor(base_view.plant_hub_cost, key="ed_ph", use_container_width=True)
        with c2:
            st.caption("Hub → CFA (₹/kL)")
            pending["hub_cfa_cost"] = st.data_editor(base_view.hub_cfa_cost[HUBS], key="ed_hc", use_container_width=True)
    with etabs[3]:
        c1, c2 = st.columns(2)
        with c1:
            st.caption("CFA opening stock (kL)")
            pending["opening_cfa"] = st.data_editor(base_view.opening_cfa, key="ed_oc",
                use_container_width=True, height=320, disabled=["sku", "cfa"])
        with c2:
            st.caption("Hub opening stock (kL)")
            pending["opening_hub"] = st.data_editor(base_view.opening_hub, key="ed_oh",
                use_container_width=True, height=320, disabled=["sku", "hub"])
    with etabs[4]:
        st.caption("Penalty ₹/kL for under-supplying, and contractual protection flags. " + TT["penalty"])
        sv = base_view.sku[["pack", "penalty", "contractual"]].reset_index()
        esv = st.data_editor(sv, key="ed_sku", use_container_width=True, height=360,
                             disabled=["sku", "pack"])
        pending["sku"] = esv
    with etabs[5]:
        st.caption("Tier fill-rate targets (0–1). " + TT["sl"])
        pending["service_levels"] = st.data_editor(base_view.service_levels.reset_index(),
                                                   key="ed_sl", disabled=["tier"])
        pending["service_levels"] = pending["service_levels"].set_index("tier")
    with etabs[6]:
        st.caption("Computed hub safety-stock targets (editable override, kL). " + TT["hubss"])
        if res:
            hub_tab = res["hub_norms"][["sku", "hub", "safety_stock_kl"]]
        else:
            from core.analytics import classify_tiers as _ct, cfa_norms as _cn, hub_norms as _hn
            _t = _ct(base_view); hub_tab = _hn(base_view, _cn(base_view, _t))[["sku", "hub", "safety_stock_kl"]]
        ss.setdefault("hub_ss_override", None)
        eh = st.data_editor(hub_tab, key="ed_hss", use_container_width=True, height=320,
                            disabled=["sku", "hub"])
        if st.checkbox("Use these hub SS values on next run", value=ss.hub_ss_override is not None):
            ss.hub_ss_override = eh
        else:
            ss.hub_ss_override = None

    st.divider()
    b1, b2, b3, b4, b5 = st.columns(5)
    if b1.button("💾 Save Changes", type="primary", use_container_width=True):
        ss.edits.update({k2: v for k2, v in pending.items()})
        st.success("Edits saved to this scenario. Press Run Plan to re-optimize.")
    if b2.button("↩ Discard Changes", use_container_width=True):
        st.rerun()
    if b3.button("🔄 Reset to Uploaded Data", use_container_width=True):
        ss.edits = {}
        st.success("All edits cleared — inputs restored to the uploaded workbook.")
        st.rerun()
    if b4.button("🎛 Reset Scenario levers", use_container_width=True):
        ss.levers = {}
        st.rerun()
    if b5.button("▶ Run Plan", key="run2", type="primary", use_container_width=True):
        ss.edits.update({k2: v for k2, v in pending.items()})
        ss["_run_requested"] = True
        st.rerun()

# ================================================================== SCENARIO MANAGER
with tabs[2]:
    st.subheader("🎛 Scenario Manager")
    st.caption("Five slots. Select a slot in the sidebar, apply edits/levers, Run. Then compare any two below.")
    rows = []
    for name in SCENARIO_SLOTS:
        r = ss.scenarios.get(name)
        if r:
            s = r["summary"]
            rows.append({"Scenario": name, "Status": "✓ solved",
                         "Levers": ", ".join(f"{k}={v}" for k, v in r["edits"].items()
                                             if not isinstance(v, pd.DataFrame)) or "—",
                         "Edited tables": ", ".join(k for k, v in r["edits"].items()
                                                    if isinstance(v, pd.DataFrame)) or "—",
                         "Total ₹ cr": round(s["total_cost"]/1e7, 3),
                         "Fill %": round(s["fill_rate"]*100, 2),
                         "Unmet kL": round(s["unmet_kl"], 1),
                         "Inventory kL": round(s["inventory_norm_kl"], 0)})
        else:
            rows.append({"Scenario": name, "Status": "not run", "Levers": "", "Edited tables": "",
                         "Total ₹ cr": None, "Fill %": None, "Unmet kL": None, "Inventory kL": None})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    solved = [n for n in SCENARIO_SLOTS if n in ss.scenarios]
    if len(solved) >= 2:
        c1, c2 = st.columns(2)
        s_a = c1.selectbox("Compare", solved, 0)
        s_b = c2.selectbox("with", solved, 1)
        A, B = ss.scenarios[s_a], ss.scenarios[s_b]
        comp = pd.DataFrame({s_a: {kk: vv/1e7 for kk, vv in A["plan"].costs.items()},
                             s_b: {kk: vv/1e7 for kk, vv in B["plan"].costs.items()}}).round(3)
        comp[f"Δ ({s_b} − {s_a})"] = (comp[s_b] - comp[s_a]).round(3)
        st.dataframe(comp, use_container_width=True)
        m1, m2, m3 = st.columns(3)
        m1.metric("Fill rate", f"{B['summary']['fill_rate']*100:.2f}%",
                  f"{(B['summary']['fill_rate']-A['summary']['fill_rate'])*100:+.2f} pp vs {s_a}")
        m2.metric("Total cost", f"₹{B['summary']['total_cost']/1e7:,.2f} cr",
                  f"{(B['summary']['total_cost']-A['summary']['total_cost'])/1e7:+,.2f} cr",
                  delta_color="inverse")
        m3.metric("Unmet", f"{B['summary']['unmet_kl']:,.1f} kL",
                  f"{B['summary']['unmet_kl']-A['summary']['unmet_kl']:+,.1f} kL", delta_color="inverse")
    else:
        st.info("Solve at least two scenarios to unlock comparison.")

# ================================================================== NORMS
with tabs[3]:
    if not res:
        st.caption("Run the plan first.")
    else:
        st.subheader("Inventory norms — SKU × CFA")
        st.caption("Safety stock = protection against forecast error and lead-time variability. " + TT["safety"])
        f1, f2, f3 = st.columns(3)
        sel_sku = f1.multiselect("SKU", sorted(res["norms"].sku.unique()))
        sel_cfa = f2.multiselect("CFA", sorted(res["norms"].cfa.unique()))
        sel_tier = f3.multiselect("Tier", list("ABCD"))
        nn = res["norms"]
        if sel_sku: nn = nn[nn.sku.isin(sel_sku)]
        if sel_cfa: nn = nn[nn.cfa.isin(sel_cfa)]
        if sel_tier: nn = nn[nn.tier.isin(sel_tier)]
        st.dataframe(nn, use_container_width=True, height=400)
        st.subheader("Hub norms (98% service, risk-pooled)")
        st.dataframe(res["hub_norms"], use_container_width=True, height=260)

# ================================================================== PRODUCTION
with tabs[4]:
    if not res:
        st.caption("Run the plan first.")
    else:
        st.subheader("Production plan — SKU × plant")
        st.caption(TT["batch"])
        pr = plan.production.assign(tier=lambda x: x.sku.map(res["tiers"]["tier"]))
        st.dataframe(pr.sort_values(["plant", "line", "sku"]), use_container_width=True, height=400)
        st.subheader("Totals by line (kL) vs capacity")
        tot = pr.pivot_table(index="line", columns="plant", values="kl", aggfunc="sum").fillna(0)
        cap = res["data"].plants[LINE_GROUPS].T
        show = pd.concat({"produced": tot, "capacity": cap}, axis=1).fillna(0).round(0)
        st.dataframe(show, use_container_width=True)

# ================================================================== NETWORK & MAP
COORD = {"BOM": (19.08, 72.88), "AHM_P": (23.02, 72.57), "KOL_P": (22.57, 88.36),
         "MHW": (19.45, 73.20), "MHE": (22.80, 88.20),
         "Guwahati CFA": (26.14, 91.74), "Kolkata CFA": (22.45, 88.55),
         "Jamshedpur CFA": (22.80, 86.20), "Kanpur CFA": (26.45, 80.33),
         "Haryana CFA": (28.46, 77.03), "Rajpura CFA": (30.48, 76.59),
         "Bhiwandi CFA": (19.30, 73.06), "Bangalore CFA": (12.97, 77.59),
         "Ahmedabad CFA": (23.20, 72.40), "Hyderabad CFA": (17.38, 78.48)}
with tabs[5]:
    if not res:
        st.caption("Run the plan first.")
    else:
        c1, c2 = st.columns([1, 2])
        with c1:
            st.subheader("Plant → Hub (kL)")
            st.dataframe(plan.plant_hub.round(1), use_container_width=True, hide_index=True)
            st.subheader("Hub ending stock")
            hp = plan.hub_position.groupby("hub")[["opening_kl", "ss_target_kl", "ending_kl", "shortfall_kl"]].sum().round(0)
            st.dataframe(hp, use_container_width=True)
        with c2:
            if PLOTLY:
                st.subheader("Material flow — Sankey")
                hubflow = plan.hub_cfa.groupby(["hub", "cfa"])["kl"].sum().reset_index()
                nodes = PLANTS + HUBS + sorted(hubflow.cfa.unique())
                nid = {n: i for i, n in enumerate(nodes)}
                src, dst, val = [], [], []
                for _, r0 in plan.plant_hub.iterrows():
                    src.append(nid[r0["plant"]]); dst.append(nid[r0["hub"]]); val.append(r0["kl"])
                for _, r0 in hubflow.iterrows():
                    src.append(nid[r0["hub"]]); dst.append(nid[r0["cfa"]]); val.append(r0["kl"])
                colors = ["#C55A11"]*3 + ["#1F4E79"]*2 + ["#70AD47"]*len(hubflow.cfa.unique())
                fig = go.Figure(go.Sankey(
                    node=dict(label=[n.replace(" CFA", "") for n in nodes], color=colors, pad=12, thickness=14),
                    link=dict(source=src, target=dst, value=val,
                              color="rgba(91,155,213,0.35)")))
                fig.update_layout(height=430, margin=dict(t=10, b=10, l=10, r=10))
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("Install plotly for the Sankey view (pip install plotly). Tables shown instead.")
        st.subheader("Hub → CFA dispatches by SKU")
        st.dataframe(plan.hub_cfa.round(2), use_container_width=True, height=300)

        if PLOTLY:
            st.subheader("Network map — India")
            fig = go.Figure()
            hubflow = plan.hub_cfa.groupby(["hub", "cfa"])["kl"].sum().reset_index()
            pk = {"BOM": "BOM", "AHM": "AHM_P", "KOL": "KOL_P"}
            for _, r0 in plan.plant_hub.iterrows():
                a, b = COORD[pk[r0["plant"]]], COORD[r0["hub"]]
                fig.add_trace(go.Scattergeo(lat=[a[0], b[0]], lon=[a[1], b[1]], mode="lines",
                    line=dict(width=max(1, min(8, r0["kl"]/400)), color="#C55A11"),
                    opacity=0.7, showlegend=False,
                    hoverinfo="text", text=f"{r0['plant']}→{r0['hub']}: {r0['kl']:,.0f} kL"))
            for _, r0 in hubflow.iterrows():
                a, b = COORD[r0["hub"]], COORD[r0["cfa"]]
                fig.add_trace(go.Scattergeo(lat=[a[0], b[0]], lon=[a[1], b[1]], mode="lines",
                    line=dict(width=max(1, min(6, r0["kl"]/250)), color="#1F4E79"),
                    opacity=0.5, showlegend=False,
                    hoverinfo="text", text=f"{r0['hub']}→{r0['cfa']}: {r0['kl']:,.0f} kL"))
            for grp, keys, col, sym in [("Plants", ["BOM", "AHM_P", "KOL_P"], "#C55A11", "square"),
                                        ("Hubs", HUBS, "#1F4E79", "diamond"),
                                        ("CFAs", [k2 for k2 in COORD if k2.endswith("CFA")], "#70AD47", "circle")]:
                fig.add_trace(go.Scattergeo(
                    lat=[COORD[k2][0] for k2 in keys], lon=[COORD[k2][1] for k2 in keys],
                    text=[k2.replace("_P", "").replace(" CFA", "") for k2 in keys],
                    mode="markers+text", textposition="top center", name=grp,
                    marker=dict(size=11, color=col, symbol=sym)))
            fig.update_geos(scope="asia", lataxis_range=[6, 34], lonaxis_range=[66, 96],
                            showcountries=True, countrycolor="#CCCCCC", landcolor="#F7F7F2")
            fig.update_layout(height=560, margin=dict(t=10, b=10, l=10, r=10),
                              legend=dict(orientation="h"))
            st.plotly_chart(fig, use_container_width=True)

# ================================================================== OPTIMIZATION SUMMARY
with tabs[6]:
    if not res:
        st.caption("Run the plan first.")
    else:
        st.subheader("🧭 Optimization Summary — constraint compliance")
        for c0 in constraint_summary(plan, res["data"]):
            ok = c0["status"].startswith("✅")
            soft = c0["status"].startswith("⚠️")
            box = st.success if ok else (st.warning if soft else st.error)
            msg = f"**{c0['constraint']}** — {c0['status']}\n\n{c0['detail']}"
            if not ok:
                msg += f"\n\n*Impact:* {c0['impact']}  \n*Action:* {c0['action']}"
            box(msg)
        st.caption("These checks re-verify the solved plan independently of the optimizer — "
                   "the same 10-point test suite ships in tests/test_engine.py.")

# ================================================================== SHORTAGES
with tabs[7]:
    if not res:
        st.caption("Run the plan first.")
    elif not len(plan.unmet):
        st.success("No unmet demand and no hub buffer shortfalls in this scenario. 🎉")
    else:
        st.subheader("⚠️ Every shortage, explained")
        for e in shortage_explanations(plan, res["data"], res["tiers"]):
            with st.container(border=True):
                st.markdown(f"**{e['sku']} — {e['cfa']}** · {e['kl']} kL · Tier {e['tier']}")
                st.markdown(f"**Why:** {e['reason']}")
                st.markdown(f"**Impact:** {e['impact']}")
                st.markdown(f"**Suggested action:** {e['action']}")
        st.dataframe(plan.unmet.sort_values("penalty_cost", ascending=False),
                     use_container_width=True)

# ================================================================== SENSITIVITY
with tabs[8]:
    st.subheader("📈 Sensitivity Analysis")
    st.caption("How robust is the plan? Sweep one lever across a range; each point is a full re-optimization.")
    c1, c2, c3 = st.columns([1.2, 2, 1])
    param = c1.selectbox("Lever", ["Demand", "Capacity", "Transport cost", "Penalty cost", "Service level"])
    lo, hi = c2.slider("Range (multiplier)", 0.5, 2.0, (0.8, 1.2), 0.05)
    npts = c3.selectbox("Points", [5, 7, 9], 0)
    if st.button("Run sweep", type="primary"):
        pts = list(np.round(np.linspace(lo, hi, npts), 3))
        with st.spinner(f"Running {npts} optimizations…"):
            sw = sensitivity_sweep(ss.base_data, param, pts,
                                   include_cfa_ss=ss.settings["include_cfa_ss"],
                                   hub_short_cost=ss.settings["hub_short_cost"])
        ss["last_sweep"] = (param, sw)
    if "last_sweep" in ss:
        param0, sw = ss["last_sweep"]
        st.markdown(f"**Sweep: {param0}**")
        a, b = st.columns(2)
        a.line_chart(sw.set_index("multiplier")[["total_cost_cr"]], height=260)
        a.caption("Total cost (₹ crore)")
        b.line_chart(sw.set_index("multiplier")[["fill_rate_pct"]], height=260)
        b.caption("Fill rate (%)")
        a2, b2 = st.columns(2)
        a2.line_chart(sw.set_index("multiplier")[["penalty_cr"]], height=240)
        a2.caption("Penalty cost (₹ crore)")
        b2.line_chart(sw.set_index("multiplier")[["inventory_norm_kl"]], height=240)
        b2.caption("Network safety stock (kL)")
        st.dataframe(sw, use_container_width=True, hide_index=True)

# ================================================================== DATA QUALITY
with tabs[9]:
    st.subheader("🧪 Data validation")
    if not issues:
        st.success("No data quality issues detected.")
    for i0 in issues:
        {"error": st.error, "warning": st.warning, "info": st.info}[i0["level"]](
            f"**{i0['area']}** — {i0['message']}")
    st.caption("Errors block the run; warnings are auto-handled safely (negatives floored to zero); "
               "info items are for awareness. The app never crashes on bad input — it tells you what's wrong.")
    d0 = apply_overrides(ss.base_data, {**ss.edits, **ss.levers})
    st.write(f"- SKU×CFA demand rows: **{len(d0.jan_forecast)}** · SKUs: **{len(d0.sku)}** · "
             f"CFAs: **{d0.hub_cfa_cost.shape[0]}** · Plants: **{len(d0.plants)}** · Hubs: **2**")

# ================================================================== DOWNLOADS
with tabs[10]:
    st.subheader("⬇ Reports")
    if not res:
        st.caption("Run the plan first.")
    else:
        recs0 = recommendations(plan, res["data"], res["tiers"])
        cons0 = constraint_summary(plan, res["data"])
        c1, c2 = st.columns(2)
        with c1:
            st.download_button("📦 Inventory Norms workbook",
                norms_workbook(res["norms"], res["hub_norms"], res["tiers"], res["xyz"], res["fe"]).getvalue(),
                f"Levisol_Inventory_Norms_{ss.active.replace(' ', '')}.xlsx", use_container_width=True)
            st.download_button("🏭 Production & Distribution Plan workbook",
                plan_workbook(plan, res["data"], res["tiers"]).getvalue(),
                f"Levisol_Plan_{ss.active.replace(' ', '')}.xlsx", use_container_width=True)
        with c2:
            st.download_button("📊 Executive Summary (KPIs + recommendations + constraints)",
                exec_summary_workbook(plan, res["data"], res["tiers"], recs0, cons0).getvalue(),
                f"Levisol_Executive_Summary_{ss.active.replace(' ', '')}.xlsx", use_container_width=True)
            if len(ss.scenarios) >= 2:
                st.download_button("🎛 Scenario Comparison report",
                    scenario_comparison_workbook(ss.scenarios).getvalue(),
                    "Levisol_Scenario_Comparison.xlsx", use_container_width=True)
            else:
                st.caption("Solve ≥2 scenarios to enable the comparison report.")
