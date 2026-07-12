"""
V2 - Input validation layer.
Runs after every load/edit and before every optimization run.
Returns a list of {level, area, message} dicts; never raises for data issues.
Levels: "error" (blocks a sensible run), "warning" (run allowed, flagged),
        "info" (worth knowing).
"""
import numpy as np
import pandas as pd
from .data_loader import CaseData, LINE_GROUPS, PLANTS, HUBS


def _msg(level, area, message):
    return {"level": level, "area": area, "message": message}


def validate_case_data(data: CaseData) -> list:
    issues = []
    sku_master = set(data.sku.index)

    # ---- SKU master integrity
    dup = data.sku.index[data.sku.index.duplicated()].tolist()
    if dup:
        issues.append(_msg("error", "SKU master", f"Duplicate SKUs in portfolio: {sorted(set(dup))}"))
    if data.sku["penalty"].isna().any() or (data.sku["penalty"] <= 0).any():
        bad = data.sku[(data.sku['penalty'].isna()) | (data.sku['penalty'] <= 0)].index.tolist()
        issues.append(_msg("warning", "Penalties", f"Missing/zero penalty cost for {bad} — these SKUs would never be protected in a shortage."))

    # ---- demand
    jf = data.jan_forecast
    neg = jf[jf["qty"] < 0]
    if len(neg):
        issues.append(_msg("warning", "Demand", f"{len(neg)} negative demand rows (e.g. {neg.iloc[0]['sku']} @ {neg.iloc[0]['cfa']}) — treated as 0."))
    unknown = sorted(set(jf["sku"]) - sku_master)
    if unknown:
        issues.append(_msg("error", "Demand", f"Demand rows for SKUs missing from the portfolio: {unknown[:5]}{'…' if len(unknown) > 5 else ''}"))
    missing = sorted(sku_master - set(jf["sku"]))
    if missing:
        issues.append(_msg("info", "Demand", f"{len(missing)} portfolio SKUs have no January demand rows (OK if discontinued): {missing[:5]}…"))

    # ---- costs
    if (data.plant_hub_cost.values < 0).any():
        issues.append(_msg("error", "Transport", "Negative plant→hub transport cost found."))
    if (data.hub_cfa_cost[HUBS].values < 0).any():
        issues.append(_msg("error", "Transport", "Negative hub→CFA transport cost found."))
    if (data.plants["prod_cost"] <= 0).any():
        issues.append(_msg("error", "Production", "Non-positive production cost at a plant."))

    # ---- capacities
    caps = data.plants[LINE_GROUPS]
    if (caps.values < 0).any():
        issues.append(_msg("error", "Capacity", "Negative line capacity found."))
    line_dem = jf.assign(line=jf["sku"].map(data.sku["line"])).groupby("line")["qty"].sum()
    for g in LINE_GROUPS:
        cap_g = float(caps[g].sum())
        dem_g = float(line_dem.get(g, 0.0))
        if dem_g > cap_g:
            issues.append(_msg("warning", "Capacity",
                f"Line {g}: national capacity {cap_g:,.0f} kL < demand {dem_g:,.0f} kL — "
                f"the plan will shed lowest-priority volume (tier D→A, contractual protected)."))

    # ---- inventory
    for name, df, col in [("CFA opening stock", data.opening_cfa, "qty"),
                          ("Hub opening stock", data.opening_hub, "qty")]:
        if (df[col] < 0).any():
            n = int((df[col] < 0).sum())
            issues.append(_msg("warning", "Inventory", f"{n} negative {name} rows — treated as 0."))

    # ---- lead times
    lt_keys = set(zip(data.leadtime["sku"], data.leadtime["cfa"]))
    dem_keys = set(zip(jf["sku"], jf["cfa"]))
    miss_lt = dem_keys - lt_keys
    if miss_lt:
        issues.append(_msg("warning", "Lead times",
            f"{len(miss_lt)} SKU×CFA demand rows have no lead-time record — norms fall back to defaults."))
    if (data.leadtime[["lt_plant_hub", "lt_hub_cfa", "lt_prod"]].values < 0).any():
        issues.append(_msg("error", "Lead times", "Negative lead time found."))

    # ---- service levels
    sl = data.service_levels["fill_rate"]
    if ((sl <= 0) | (sl >= 1)).any():
        issues.append(_msg("error", "Service levels", "Fill-rate targets must be between 0 and 100%."))

    # ---- carried-over loader warnings
    for w in data.warnings:
        issues.append(_msg("info", "Load", w))
    return issues


def sanitize(data: CaseData) -> CaseData:
    """Apply safe fixes for warning-level issues (floors negatives). Non-destructive copy is the caller's job."""
    data.jan_forecast["qty"] = data.jan_forecast["qty"].clip(lower=0)
    data.opening_cfa["qty"] = data.opening_cfa["qty"].clip(lower=0)
    data.opening_hub["qty"] = data.opening_hub["qty"].clip(lower=0)
    data.plants[LINE_GROUPS] = data.plants[LINE_GROUPS].clip(lower=0)
    return data
