"""
V2 - Scenario layer.
Applies in-app edits/overrides to a copy of the uploaded CaseData, defines the
one-click scenario presets, and runs the unchanged analytical pipeline.
The uploaded workbook is the initial dataset only; all editing happens here.
"""
import copy
import numpy as np
import pandas as pd
from .data_loader import CaseData, LINE_GROUPS, HUBS, PLANTS
from .analytics import classify_tiers, xyz_classes, forecast_errors, cfa_norms, hub_norms
from .optimizer import optimize_plan
from .validation import sanitize

SCENARIO_SLOTS = ["Baseline", "Scenario 1", "Scenario 2", "Scenario 3", "Scenario 4"]

QUICK_SCENARIOS = {
    "Demand +10%":        {"demand_mult": 1.10},
    "Demand -10%":        {"demand_mult": 0.90},
    "Mumbai shutdown":    {"plant_down": "BOM"},
    "Fuel cost +20%":     {"transport_mult": 1.20},
    "Capacity -15%":      {"capacity_mult": 0.85},
    "Transport inflation +35%": {"transport_mult": 1.35},
}


def apply_overrides(base: CaseData, edits: dict) -> CaseData:
    """edits keys (all optional):
       demand (df sku,cfa,qty) · plants (df like data.plants) ·
       plant_hub_cost · hub_cfa_cost · opening_cfa · opening_hub · sku ·
       service_levels · demand_mult · capacity_mult · transport_mult ·
       plant_down (plant code)"""
    d = copy.deepcopy(base)
    if edits.get("demand") is not None:
        e = edits["demand"].set_index(["sku", "cfa"])["qty"]
        idx = d.jan_forecast.set_index(["sku", "cfa"]).index
        d.jan_forecast["qty"] = e.reindex(idx).fillna(0).values
    if edits.get("plants") is not None:
        d.plants.loc[:, LINE_GROUPS + ["prod_cost"]] = \
            edits["plants"][LINE_GROUPS + ["prod_cost"]].values
    if edits.get("plant_hub_cost") is not None:
        d.plant_hub_cost.loc[:, HUBS] = edits["plant_hub_cost"][HUBS].values
    if edits.get("hub_cfa_cost") is not None:
        d.hub_cfa_cost.loc[:, HUBS] = edits["hub_cfa_cost"][HUBS].values
    if edits.get("opening_cfa") is not None:
        d.opening_cfa["qty"] = edits["opening_cfa"]["qty"].values
    if edits.get("opening_hub") is not None:
        d.opening_hub["qty"] = edits["opening_hub"]["qty"].values
    if edits.get("sku") is not None:      # penalty + contractual editable
        d.sku["penalty"] = edits["sku"]["penalty"].values
        d.sku["contractual"] = edits["sku"]["contractual"].astype(bool).values
    if edits.get("service_levels") is not None:
        d.service_levels["fill_rate"] = edits["service_levels"]["fill_rate"].values
    # multipliers / shutdown
    d.jan_forecast["qty"] *= float(edits.get("demand_mult", 1.0))
    d.plants[LINE_GROUPS] = d.plants[LINE_GROUPS] * float(edits.get("capacity_mult", 1.0))
    tm = float(edits.get("transport_mult", 1.0))
    d.plant_hub_cost.loc[:, HUBS] = d.plant_hub_cost[HUBS].values * tm
    d.hub_cfa_cost.loc[:, HUBS] = d.hub_cfa_cost[HUBS].values * tm
    if edits.get("plant_down"):
        d.plants.loc[edits["plant_down"], LINE_GROUPS] = 0.0
    return sanitize(d)


def run_scenario(base: CaseData, edits: dict, include_cfa_ss=False,
                 hub_short_cost=75000.0, hub_ss_override: pd.DataFrame = None) -> dict:
    """Full pipeline on an edited copy. Returns everything a tab needs."""
    d = apply_overrides(base, edits or {})
    tiers = classify_tiers(d)
    xyz = xyz_classes(d)
    fe = forecast_errors(d)
    norms = cfa_norms(d, tiers)
    hn = hub_norms(d, norms)
    if hub_ss_override is not None and len(hub_ss_override):
        o = hub_ss_override.set_index(["sku", "hub"])["safety_stock_kl"]
        hn = hn.copy()
        idx = hn.set_index(["sku", "hub"]).index
        hn["safety_stock_kl"] = o.reindex(idx).fillna(
            hn.set_index(["sku", "hub"])["safety_stock_kl"]).values
    plan = optimize_plan(d, tiers, hn, norms, include_cfa_ss=include_cfa_ss,
                         hub_short_cost=hub_short_cost)
    inv_kl = float(norms["safety_stock_kl"].sum() + hn["safety_stock_kl"].sum())
    return {"data": d, "edits": edits or {}, "tiers": tiers, "xyz": xyz, "fe": fe,
            "norms": norms, "hub_norms": hn, "plan": plan,
            "summary": {"total_cost": plan.costs["total"],
                        "fill_rate": plan.kpis["fill_rate"],
                        "production_kl": plan.kpis["total_production_kl"],
                        "unmet_kl": float(plan.unmet["kl"].sum()) if len(plan.unmet) else 0.0,
                        "inventory_norm_kl": inv_kl}}


def sensitivity_sweep(base: CaseData, param: str, points: list,
                      include_cfa_ss=False, hub_short_cost=75000.0) -> pd.DataFrame:
    """Re-run the pipeline across a multiplier grid for one lever."""
    key = {"Demand": "demand_mult", "Capacity": "capacity_mult",
           "Transport cost": "transport_mult"}.get(param)
    rows = []
    for x in points:
        edits = {key: x} if key else {}
        d = apply_overrides(base, edits)
        if param == "Penalty cost":
            d.sku["penalty"] = base.sku["penalty"].values * x
        if param == "Service level":
            d.service_levels["fill_rate"] = (base.service_levels["fill_rate"].values * x).clip(0.5, 0.999)
        tiers = classify_tiers(d)
        norms = cfa_norms(d, tiers)
        hn = hub_norms(d, norms)
        plan = optimize_plan(d, tiers, hn, norms, include_cfa_ss=include_cfa_ss,
                             hub_short_cost=hub_short_cost)
        rows.append({"multiplier": x,
                     "total_cost_cr": plan.costs["total"] / 1e7,
                     "fill_rate_pct": plan.kpis["fill_rate"] * 100,
                     "penalty_cr": (plan.costs["penalty_unmet"] + plan.costs["hub_ss_shortfall"]) / 1e7,
                     "inventory_norm_kl": norms["safety_stock_kl"].sum() + hn["safety_stock_kl"].sum(),
                     "production_kl": plan.kpis["total_production_kl"]})
    return pd.DataFrame(rows)
