"""Integrity tests for the Levisol planning engine (run: python3 -m tests.test_engine [xlsx])."""
import sys
sys.path.insert(0, ".")
import numpy as np
import pandas as pd
from core.data_loader import load_case_data, LINE_GROUPS, PLANTS
from core.analytics import classify_tiers, cfa_norms, hub_norms
from core.optimizer import optimize_plan, BATCH

def run(path):
    data = load_case_data(path)
    tiers = classify_tiers(data)
    norms = cfa_norms(data, tiers)
    hn = hub_norms(data, norms)
    plan = optimize_plan(data, tiers, hn, norms)
    ok = True
    def check(name, cond, detail=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name, detail)
        ok = ok and cond

    pr = plan.production
    # 1. batch multiples
    check("batch multiples of 25", bool(((pr.kl % BATCH).abs() < 1e-6).all()))
    # 2. line capacity
    viol = []
    for p in PLANTS:
        for g in LINE_GROUPS:
            cap = float(data.plants.loc[p, g])
            usedv = pr[(pr.plant == p) & (pr.line == g)].kl.sum()
            if usedv > cap + 1e-6:
                viol.append((p, g, usedv, cap))
    check("line capacities respected", not viol, str(viol))
    # 3. flow balance: production == plant->hub flows
    check("plant->hub == production",
          abs(pr.kl.sum() - plan.plant_hub.kl.sum()) < 1e-4,
          f"{pr.kl.sum():.1f} vs {plan.plant_hub.kl.sum():.1f}")
    # 4. demand satisfaction: served + unmet >= net requirement
    jf = data.jan_forecast.set_index(["sku", "cfa"]).qty
    oi = data.opening_cfa.set_index(["sku", "cfa"]).qty.reindex(jf.index).fillna(0)
    req = (jf - oi).clip(lower=0)
    served = plan.hub_cfa.groupby(["sku", "cfa"]).kl.sum().reindex(req.index).fillna(0)
    un = (plan.unmet[~plan.unmet.cfa.str.contains("hub SS", na=False)]
          .groupby(["sku", "cfa"]).kl.sum().reindex(req.index).fillna(0)
          if len(plan.unmet) else pd.Series(0.0, index=req.index))
    gap = (req - served - un)
    check("all net demand served or declared unmet", float(gap.max()) < 0.01,
          f"max gap {gap.max():.4f} kL")
    check("no over-service beyond requirement", float((served - req).max()) < BATCH + 0.01)
    # 5. contractual SKUs fully served
    contr = data.sku[data.sku.contractual].index
    cu = plan.unmet[plan.unmet.sku.isin(contr) & ~plan.unmet.cfa.str.contains("hub SS", na=False)] if len(plan.unmet) else []
    check("contractual SKUs fully served", len(cu) == 0)
    # 6. cost reconciliation
    cp = sum(r.kl * data.plants.loc[r.plant, "prod_cost"] for r in pr.itertuples())
    check("production cost reconciles", abs(cp - plan.costs["production"]) < 1.0)
    cph = sum(r.kl * data.plant_hub_cost.loc[r.plant, r.hub] for r in plan.plant_hub.itertuples())
    check("plant-hub freight reconciles", abs(cph - plan.costs["transport_plant_hub"]) < 1.0)
    chc = sum(r.kl * data.hub_cfa_cost.loc[r.cfa, r.hub] for r in plan.hub_cfa.itertuples())
    check("hub-CFA freight reconciles", abs(chc - plan.costs["transport_hub_cfa"]) < 1.0)
    # 7. hub ending stock never negative
    check("hub ending stock >= 0", bool((plan.hub_position.ending_kl > -1e-6).all()))
    print("\nALL CHECKS PASSED" if ok else "\nSOME CHECKS FAILED")
    return ok

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/sessions/laughing-funny-dirac/mnt/uploads/Supply Chain Supporting Data.xlsx"
    sys.exit(0 if run(path) else 1)
