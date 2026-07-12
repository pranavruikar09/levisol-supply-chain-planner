"""End-to-end pipeline: load -> tiers -> norms -> optimize -> print summary."""
import sys, json
import pandas as pd
from core.data_loader import load_case_data
from core.analytics import classify_tiers, xyz_classes, forecast_errors, cfa_norms, hub_norms
from core.optimizer import optimize_plan

path = sys.argv[1] if len(sys.argv) > 1 else "/sessions/laughing-funny-dirac/mnt/uploads/Supply Chain Supporting Data.xlsx"
data = load_case_data(path)
print("warnings:", data.warnings)
tiers = classify_tiers(data)
print(tiers.tier.value_counts().to_dict())
xyz = xyz_classes(data)
fe = forecast_errors(data)
norms = cfa_norms(data, tiers)
hn = hub_norms(data, norms)
print("norms rows:", len(norms), " hub norms rows:", len(hn))
print(norms[['safety_stock_kl','reorder_point_kl','days_of_cover']].describe().round(2))
plan = optimize_plan(data, tiers, hn, norms, include_cfa_ss=False)
print("\nCOSTS:", {k: round(v/1e7,3) for k,v in plan.costs.items()}, "(Rs crore)")
print("KPIs: fill", round(plan.kpis['fill_rate']*100,2), "% | prod", round(plan.kpis['total_production_kl'],0), "kL")
print("util:", {k: round(v,2) for k,v in plan.kpis['utilization'].items()})
print("unmet rows:", len(plan.unmet), "unmet kl:", 0 if not len(plan.unmet) else round(plan.unmet.kl.sum(),1))
if len(plan.unmet): print(plan.unmet.head(10))
print("plan warnings:", plan.warnings)
# stash for later steps
import pickle
with open('/tmp/pipeline.pkl','wb') as f:
    pickle.dump({'tiers':tiers,'xyz':xyz,'fe':fe,'norms':norms,'hn':hn,'plan':plan}, f)
