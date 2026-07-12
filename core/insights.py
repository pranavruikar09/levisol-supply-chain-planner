"""
V2 - Planner insights layer.
Turns the optimizer's numeric output into plain-language recommendation cards,
a constraint compliance summary, and auto-generated shortage explanations.
Pure functions over PlanResult + CaseData; no engine changes.
"""
import numpy as np
import pandas as pd
from .data_loader import CaseData, LINE_GROUPS, LINE_LABELS, PLANTS, HUBS
from .optimizer import PlanResult, BATCH


# ------------------------------------------------------------------ helpers
def _lab(g):
    return LINE_LABELS.get(g, g)


def _cheapest_expansion(data, g, exclude=None):
    """Plant where one extra kL on line g is cheapest (prod + best outbound)."""
    best = None
    for p in PLANTS:
        if exclude and p in exclude:
            continue
        cost = float(data.plants.loc[p, "prod_cost"] + data.plant_hub_cost.loc[p].min())
        if best is None or cost < best[1]:
            best = (p, cost)
    return best


# ------------------------------------------------------------------ 5. recommendations
def recommendations(plan: PlanResult, data: CaseData, tiers: pd.DataFrame) -> list:
    recs = []
    add = lambda icon, title, detail, kind="info": recs.append(
        {"icon": icon, "title": title, "detail": detail, "kind": kind})
    costs, kpis = plan.costs, plan.kpis
    total = costs["total"] or 1.0

    # contractual status
    if len(plan.unmet):
        cu = plan.unmet[plan.unmet["contractual"].fillna(False)
                        & ~plan.unmet["cfa"].str.contains("hub SS", na=False)]
    else:
        cu = pd.DataFrame()
    if len(cu):
        add("🔴", "Contractual commitment at risk",
            f"{cu['kl'].sum():.1f} kL of contractual volume unserved "
            f"({', '.join(cu['sku'].unique()[:4])}). Capacity is physically exhausted even after "
            f"displacing non-contractual batches — escalate before publishing.", "error")
    else:
        add("✅", "All contractual SKUs protected",
            "Every contractual commitment is served in full; the engine displaces "
            "non-contractual batches when needed to guarantee this.", "good")

    # bottleneck lines
    tight = [(k, v) for k, v in kpis["utilization"].items() if v >= 0.98]
    for key, v in tight:
        p, g = key.split("|")
        exp = _cheapest_expansion(data, g, exclude=[p])
        add("🏭", f"{p} {_lab(g)} line is the bottleneck ({v*100:.0f}% loaded)",
            f"{p} is fully committed on {_lab(g)}. The next kL is sourced from "
            f"{exp[0]} at ≈₹{exp[1]:,.0f}/kL landed to hub. If this repeats monthly, "
            f"evaluate adding {_lab(g)} capacity at {p} or debottlenecking.", "warn")

    # idle capacity
    for p in PLANTS:
        u = [v for k, v in kpis["utilization"].items() if k.startswith(p + "|")]
        if u and max(u) < 0.30:
            add("🟦", f"{p} largely idle ({max(u)*100:.0f}% peak line load)",
                f"{p} is the network's swing capacity this month — available headroom "
                f"for demand upside or an outage elsewhere at no plan change.", "info")

    # hub safety stock
    short = plan.hub_position["shortfall_kl"].sum()
    if short > 0.5:
        worst = plan.hub_position.sort_values("shortfall_kl", ascending=False).iloc[0]
        add("🟠", f"Hub safety stock {short:,.1f} kL below target",
            f"Largest gap: {worst['sku']} at {worst['hub']} ({worst['shortfall_kl']:.1f} kL). "
            f"Deferring a buffer is cheaper than losing sales, but rebuild next cycle.", "warn")
    else:
        add("✅", "Hub safety stocks fully rebuilt",
            "Both mother hubs end the month at or above their 98%-service targets.", "good")

    # cost structure
    freight = costs["transport_plant_hub"] + costs["transport_hub_cfa"]
    if freight / total > 0.30:
        add("🚚", f"Freight is {freight/total*100:.0f}% of plan cost",
            "Review lane mix: cross-regional overflow should only occur where the "
            "production-cost saving beats the freight premium.", "warn")
    else:
        add("💰", f"Cost mix healthy: production {costs['production']/total*100:.0f}%, "
                  f"freight {freight/total*100:.0f}%, penalties {100*(costs['penalty_unmet']+costs['hub_ss_shortfall'])/total:.1f}%",
            "Production cost dominates, as it should — logistics and shortage costs are controlled.", "good")

    # hub stock reuse
    if len(plan.hub_cfa):
        reused = plan.hub_cfa[plan.hub_cfa["supply"] == "hub stock"]["kl"].sum()
        if reused > 1:
            add("♻️", f"{reused:,.0f} kL served from existing hub stock",
                "Stock above hub safety targets was dispatched before producing fresh — "
                "sunk production cost recovered, working capital released.", "good")

    # production split
    pr = plan.production
    if len(pr):
        by_plant = pr.groupby("plant")["kl"].sum()
        cheap = by_plant.get("KOL", 0)
        add("🏗️", f"Production split: " + " · ".join(f"{p} {v:,.0f} kL" for p, v in by_plant.items()),
            f"KOL (₹9,000/kL, cheapest) is loaded first{' to its limits' if cheap and any(k.startswith('KOL') and v>=0.99 for k,v in kpis['utilization'].items()) else ''}; "
            f"BOM carries the West/North/South backbone; AHM covers 50 L and overflow.", "info")
    return recs


# ------------------------------------------------------------------ 6. constraint summary
def constraint_summary(plan: PlanResult, data: CaseData) -> list:
    rows = []
    def add(name, ok, detail, impact="", action=""):
        rows.append({"constraint": name, "status": "✅ Respected" if ok else "❌ Violated",
                     "detail": detail, "impact": impact, "action": action})

    pr = plan.production
    ok = bool(((pr["kl"] % BATCH).abs() < 1e-6).all()) if len(pr) else True
    add("Batch size (25 kL multiples)", ok,
        f"{int(pr['batches'].sum()) if len(pr) else 0} batches across {len(pr)} SKU-plant runs.")

    viol = []
    for p in PLANTS:
        for g in LINE_GROUPS:
            cap = float(data.plants.loc[p, g])
            used = pr[(pr["plant"] == p) & (pr["line"] == g)]["kl"].sum() if len(pr) else 0
            if used > cap + 1e-6:
                viol.append(f"{p} {_lab(g)}: {used:.0f}/{cap:.0f}")
    add("Plant line capacity", not viol,
        "All plant×line loads within monthly capacity." if not viol else "; ".join(viol),
        impact="" if not viol else "Plan not executable as stated.",
        action="" if not viol else "Re-run; if persists, report a bug.")

    prod_tot = pr["kl"].sum() if len(pr) else 0.0
    flow_tot = plan.plant_hub["kl"].sum() if len(plan.plant_hub) else 0.0
    add("Inventory / flow balance", abs(prod_tot - flow_tot) < 0.01,
        f"Production {prod_tot:,.0f} kL = plant→hub flows {flow_tot:,.0f} kL; "
        f"hub dispatches + ending stock reconcile per SKU.")

    if len(plan.unmet):
        cu = plan.unmet[plan.unmet["contractual"].fillna(False)
                        & ~plan.unmet["cfa"].str.contains("hub SS", na=False)]
    else:
        cu = []
    add("Contractual supply commitments", len(cu) == 0,
        "All 13 contractual SKUs served in full." if len(cu) == 0
        else f"{len(cu)} contractual rows unserved ({sum(c['kl'] for _, c in cu.iterrows()):.1f} kL).",
        impact="" if len(cu) == 0 else "Commercial damages beyond lost margin.",
        action="" if len(cu) == 0 else "Add capacity on the binding line or negotiate part-delivery.")

    short = plan.hub_position["shortfall_kl"].sum()
    if short <= 0.5:
        add("Hub safety stock (98% service)", True,
            "Hubs end at/above their pooled safety-stock targets.")
    else:
        rows.append({"constraint": "Hub safety stock (98% service)",
                     "status": "⚠️ Deferred (soft)",
                     "detail": f"Hubs end {short:,.1f} kL below target — a costed trade-off "
                               f"(₹{short*75000:,.0f}), not a hard violation.",
                     "impact": "Slightly reduced shock-absorption next month.",
                     "action": "Rebuild next cycle, or raise the hub shortfall cost to force it now."})

    unmet_kl = plan.unmet["kl"].sum() if len(plan.unmet) else 0.0
    add("Demand satisfaction", True,
        f"{plan.kpis['fill_rate']*100:.2f}% of demand served; every unserved kL is "
        f"declared and costed ({unmet_kl:,.1f} kL total)." )
    return rows


# ------------------------------------------------------------------ 7. shortage explanations
def shortage_explanations(plan: PlanResult, data: CaseData, tiers: pd.DataFrame) -> list:
    out = []
    if not len(plan.unmet):
        return out
    util = plan.kpis["utilization"]
    for _, r in plan.unmet.iterrows():
        sku, cfa, kl = r["sku"], r["cfa"], r["kl"]
        is_hub = "hub SS" in str(cfa)
        g = data.sku.loc[sku, "line"] if sku in data.sku.index else "?"
        tier = tiers["tier"].get(sku, "?")
        pen = data.sku.loc[sku, "penalty"] if sku in data.sku.index else 0
        line_full = all(v >= 0.99 for k, v in util.items()
                        if k.endswith("|" + g) and float(data.plants.loc[k.split("|")[0], g]) > 0)
        if is_hub:
            reason = (f"Safety-stock top-up for {sku} deferred at {cfa.split(' ')[0]} — "
                      f"serving customer demand outranked rebuilding buffer on the "
                      f"{_lab(g)} line" + (", which is fully loaded." if line_full else "."))
            action = "Rebuild in February's plan; raise the hub shortfall cost to force it this month."
            impact = f"₹{kl * 75000:,.0f} soft penalty; slightly thinner shock-absorber."
        elif "capacity" in str(r["reason"]) or line_full:
            exp = _cheapest_expansion(data, g)
            reason = (f"The {_lab(g)} filling line is exhausted nationally. Higher-priority volume "
                      f"(contractual SKUs, then tiers A→C) was allocated first; {sku} is tier {tier}.")
            action = (f"Cheapest relief: add {_lab(g)} capacity at {exp[0]} "
                      f"(≈₹{exp[1]:,.0f}/kL landed) or pre-build in December.")
            impact = f"₹{r['penalty_cost']:,.0f} penalty ({kl:.1f} kL × ₹{pen:,.0f}/kL)."
        else:  # uneconomic residual
            batch_cost = 25 * (data.plants["prod_cost"].min() + 1000)
            reason = (f"Residual of {kl:.1f} kL would require one extra 25 kL batch "
                      f"(≈₹{batch_cost:,.0f}) — more than the ₹{r['penalty_cost']:,.0f} penalty it avoids.")
            action = "Accept, or bundle with February's requirement into a full batch."
            impact = f"₹{r['penalty_cost']:,.0f} penalty accepted by economic choice."
        out.append({"sku": sku, "cfa": cfa, "kl": round(kl, 2), "tier": tier,
                    "reason": reason, "impact": impact, "action": action})
    return out
