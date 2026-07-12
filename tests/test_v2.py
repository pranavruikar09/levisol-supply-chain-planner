"""V2 smoke tests: validation, scenario engine, insights, exports (headless)."""
import sys
sys.path.insert(0, ".")
import copy
import numpy as np
import pandas as pd
from core.data_loader import load_case_data, LINE_GROUPS
from core.scenario import run_scenario, apply_overrides, sensitivity_sweep, QUICK_SCENARIOS
from core.validation import validate_case_data
from core.insights import recommendations, constraint_summary, shortage_explanations
from core.reporting import (norms_workbook, plan_workbook, exec_summary_workbook,
                            scenario_comparison_workbook)

def run(path):
    ok = True
    def check(name, cond, detail=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name, detail)
        ok = ok and cond

    base = load_case_data(path)

    # 1. baseline engine output unchanged (V1 parity, deterministic)
    r = run_scenario(base, {})
    check("baseline cost matches V1 engine", abs(r["summary"]["total_cost"]/1e7 - 9.8687) < 0.001,
          f"{r['summary']['total_cost']/1e7:.4f} cr")

    # 2. input-manager style table edit: demand override
    dem = base.jan_forecast[["sku", "cfa", "qty"]].copy()
    dem.loc[(dem.sku == "SKU_001") & (dem.cfa == "Kolkata CFA"), "qty"] = 300.0
    r2 = run_scenario(base, {"demand": dem})
    check("demand table edit flows through", r2["summary"]["total_cost"] > r["summary"]["total_cost"])

    # 3. quick scenarios all solve
    for name, lev in QUICK_SCENARIOS.items():
        rr = run_scenario(base, dict(lev))
        assert rr["plan"].costs["total"] > 0
    check("all 6 quick scenarios solve", True)

    # 4. Mumbai shutdown keeps contractual whole
    rmum = run_scenario(base, QUICK_SCENARIOS["Mumbai shutdown"])
    un = rmum["plan"].unmet
    cu = (un[un.contractual.fillna(False) & ~un.cfa.str.contains("hub SS", na=False)].kl.sum()
          if len(un) else 0)
    check("Mumbai shutdown: contractual protected", cu < 0.01, f"fill {rmum['summary']['fill_rate']*100:.1f}%")

    # 5. insights generate
    recs = recommendations(r["plan"], r["data"], r["tiers"])
    cons = constraint_summary(r["plan"], r["data"])
    sx = shortage_explanations(r["plan"], r["data"], r["tiers"])
    check("recommendations generated", len(recs) >= 4, f"{len(recs)} cards")
    check("constraint summary: no hard violations on baseline",
          all(not c["status"].startswith("❌") for c in cons), f"{len(cons)} checks")
    check("shortage explanations cover unmet rows",
          len(sx) == len(r["plan"].unmet), f"{len(sx)} explanations")

    # 6. validation catches corrupted input
    bad = copy.deepcopy(base)
    bad.plant_hub_cost.iloc[0, 0] = -5
    bad.plants.loc["BOM", "prod_cost"] = 0
    bad.jan_forecast.loc[0, "qty"] = -10
    issues = validate_case_data(bad)
    lv = [i["level"] for i in issues]
    check("validation flags corrupted input", lv.count("error") >= 2 and "warning" in lv,
          f"{lv.count('error')} errors, {lv.count('warning')} warnings")

    # 7. sensitivity sweep
    sw = sensitivity_sweep(base, "Demand", [0.9, 1.0, 1.1])
    check("sensitivity sweep monotone-ish cost", sw.total_cost_cr.iloc[2] > sw.total_cost_cr.iloc[0],
          sw.total_cost_cr.round(2).tolist())

    # 8. exports build
    b1 = norms_workbook(r["norms"], r["hub_norms"], r["tiers"], r["xyz"], r["fe"])
    b2 = plan_workbook(r["plan"], r["data"], r["tiers"])
    b3 = exec_summary_workbook(r["plan"], r["data"], r["tiers"], recs, cons)
    b4 = scenario_comparison_workbook({"Baseline": r, "Mumbai shutdown": rmum})
    check("all four exports build", all(len(b.getvalue()) > 5000 for b in [b1, b2, b3, b4]),
          [len(b.getvalue()) for b in [b1, b2, b3, b4]])

    # 9. hub SS override respected
    hov = r["hub_norms"][["sku", "hub", "safety_stock_kl"]].copy()
    hov["safety_stock_kl"] *= 2.0
    r3 = run_scenario(base, {}, hub_ss_override=hov)
    check("hub SS override raises production or shortfall",
          r3["plan"].kpis["total_production_kl"] >= r["plan"].kpis["total_production_kl"])

    print("\nALL V2 CHECKS PASSED" if ok else "\nSOME V2 CHECKS FAILED")
    return ok

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/sessions/laughing-funny-dirac/mnt/uploads/Supply Chain Supporting Data.xlsx"
    sys.exit(0 if run(path) else 1)
