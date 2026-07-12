"""
Levisol Planning System - Production & Distribution Optimizer
==============================================================
Pure-Python cost-minimising allocation engine (no external solver needed).

Mathematical model (what we solve):
    min  sum_p ProdCost_p * P_sp  +  sum_(p,h) TP_ph * X_sph
       + sum_(h,c) TC_hc * Y_shc  +  sum_(s,c) Pen_s * U_sc
       + HubShortCost * sum_(s,h) V_sh
    s.t. line capacity:   sum_(s in line g) P_sp <= Cap_pg          (all p,g)
         batch size:      P_sp = 25 * N_sp,  N_sp integer
         hub balance:     Open_sh + sum_p X_sph = sum_c Y_shc + End_sh
         hub safety:      End_sh >= SS_sh - V_sh
         demand:          sum_h Y_shc + U_sc >= NetReq_sc
         contractual:     U_sc = 0 for contractual SKUs (unless infeasible)

Solution strategy (exact where it matters, transparent everywhere):
  1. Net requirements  = max(0, Jan forecast - CFA opening stock [+ CFA SS rebuild]).
  2. Hub stock above its safety-stock target is 'free' supply - allocated first
     (production cost already sunk; only outbound freight is incurred).
  3. If a filling line's total requirement exceeds national capacity, the
     lowest-penalty units are pre-emptively shed (contractual SKUs are
     protected with infinite priority). This is the explicit under-serve call.
  4. Remaining requirements are solved as an exact min-cost transportation
     problem per filling line (3 plants x ~11 destinations) using successive-
     shortest-path min-cost flow. Optimal because unit costs are SKU-independent
     within a line (production cost varies by plant, not by SKU).
  5. Flows are disaggregated back to SKU level and rounded to 25 kL batches -
     rounding UP where line slack exists (excess parked at the cheapest hub as
     extra buffer), DOWN otherwise (shortfall shed from lowest-penalty drops).
"""
from dataclasses import dataclass, field
import numpy as np
import pandas as pd
from .data_loader import CaseData, LINE_GROUPS, HUBS, PLANTS

BATCH = 25.0
EPS = 1e-6


# ----------------------------------------------------------------- min-cost flow
class MinCostFlow:
    """Successive shortest path (SPFA) min-cost max-flow. Small graphs, exact."""

    def __init__(self, n):
        self.n = n
        self.graph = [[] for _ in range(n)]

    def add_edge(self, u, v, cap, cost):
        self.graph[u].append([v, cap, cost, len(self.graph[v])])
        self.graph[v].append([u, 0.0, -cost, len(self.graph[u]) - 1])

    def solve(self, s, t):
        flows = {}
        while True:
            dist = [float("inf")] * self.n
            inq = [False] * self.n
            prevv = [-1] * self.n
            preve = [-1] * self.n
            dist[s] = 0.0
            queue = [s]
            inq[s] = True
            while queue:
                u = queue.pop(0)
                inq[u] = False
                for i, (v, cap, cost, _) in enumerate(self.graph[u]):
                    if cap > EPS and dist[u] + cost < dist[v] - 1e-9:
                        dist[v] = dist[u] + cost
                        prevv[v], preve[v] = u, i
                        if not inq[v]:
                            queue.append(v)
                            inq[v] = True
            if dist[t] == float("inf"):
                break
            d = float("inf")
            v = t
            while v != s:
                d = min(d, self.graph[prevv[v]][preve[v]][1])
                v = prevv[v]
            v = t
            while v != s:
                e = self.graph[prevv[v]][preve[v]]
                e[1] -= d
                self.graph[v][e[3]][1] += d
                key = (prevv[v], v)
                flows[key] = flows.get(key, 0.0) + d
                v = prevv[v]
        return flows


@dataclass
class PlanResult:
    production: pd.DataFrame
    plant_hub: pd.DataFrame
    hub_cfa: pd.DataFrame
    unmet: pd.DataFrame
    hub_position: pd.DataFrame
    costs: dict
    kpis: dict
    warnings: list = field(default_factory=list)


def optimize_plan(data: CaseData, tiers: pd.DataFrame, hub_ss: pd.DataFrame,
                  cfa_ss: pd.DataFrame = None, include_cfa_ss: bool = False,
                  hub_short_cost: float = 75000.0) -> PlanResult:
    plants = data.plants
    tp = data.plant_hub_cost          # plant x hub
    tc = data.hub_cfa_cost            # cfa x hub
    skuinfo = data.sku
    warnings = list(data.warnings)

    # ---------- net requirements per SKU x CFA
    jf = data.jan_forecast.set_index(["sku", "cfa"])["qty"]
    oi = data.opening_cfa.set_index(["sku", "cfa"])["qty"].reindex(jf.index).fillna(0)
    req = (jf - oi).clip(lower=0)
    if include_cfa_ss and cfa_ss is not None:
        ss = cfa_ss.set_index(["sku", "cfa"])["safety_stock_kl"].reindex(jf.index).fillna(0)
        req = (jf + ss - oi).clip(lower=0)

    # ---------- hub targets / free stock / top-ups
    hss = hub_ss.set_index(["sku", "hub"])["safety_stock_kl"]
    ohub = data.opening_hub.set_index(["sku", "hub"])["qty"]
    all_hub_idx = hss.index.union(ohub.index)
    hss = hss.reindex(all_hub_idx).fillna(0)
    ohub = ohub.reindex(all_hub_idx).fillna(0)
    free = (ohub - hss).clip(lower=0)
    topup = (hss - ohub).clip(lower=0)

    # ---------- cost helpers
    prod_cost = plants["prod_cost"].to_dict()
    best_path = {}   # (plant, cfa) -> (cost, hub)
    for p in PLANTS:
        for c in tc.index:
            opts = [(prod_cost[p] + tp.loc[p, h] + tc.loc[c, h], h) for h in HUBS]
            best_path[(p, c)] = min(opts)
    cheapest_prod = {c: min(best_path[(p, c)][0] for p in PLANTS) for c in tc.index}

    # ---------- step 2: allocate free hub stock (sunk production cost)
    hub_cfa_rows = []
    req = req.copy()
    free = free.copy()
    for (s, h), f in free[free > EPS].items():
        cands = [(cheapest_prod[c] - tc.loc[c, h], c) for (s2, c) in req.index
                 if s2 == s and req[(s2, c)] > EPS and c in tc.index]
        for sav, c in sorted(cands, reverse=True):
            if f <= EPS:
                break
            if sav <= 0:
                continue
            q = min(f, req[(s, c)])
            hub_cfa_rows.append({"sku": s, "hub": h, "cfa": c, "kl": q, "supply": "hub stock"})
            req[(s, c)] -= q
            f -= q
        free[(s, h)] = f

    # ---------- build line-level problems
    line_of = skuinfo["line"].to_dict()
    penalty = skuinfo["penalty"].to_dict()
    contractual = skuinfo["contractual"].to_dict()
    tier_of = tiers["tier"].to_dict()

    prod = {}          # (sku, plant) -> kl (continuous, pre-batching)
    ship = {}          # (sku, plant, hub, cfa|None) -> kl   (cfa None = hub top-up/excess)
    unmet_rows = []
    hub_top_served = {}

    for g in LINE_GROUPS:
        skus_g = [s for s in skuinfo.index if line_of[s] == g]
        items = []   # (priority_cost, kind, sku, dest, qty)
        # Shedding priority (ascending = shed first): hub top-ups, then tiers
        # D -> A (case rule: higher tiers protected in a shortfall), penalty as
        # tie-break within a tier; contractual SKUs shed only if all else fails.
        TIER_RANK = {"D": 0, "C": 1, "B": 2, "A": 3}
        for (s, c), q in req.items():
            if s in skus_g and q > EPS:
                eff = ((9, penalty[s]) if contractual.get(s)
                       else (TIER_RANK.get(tier_of.get(s, "D"), 0), penalty[s]))
                items.append([eff, "dem", s, c, q])
        for (s, h), q in topup.items():
            if s in skus_g and q > EPS:
                items.append([(-1, hub_short_cost), "top", s, h, q])
        cap = {p: float(plants.loc[p, g]) for p in PLANTS}
        total_cap = sum(cap.values())
        total_req = sum(it[4] for it in items)

        # step 3: shed lowest-penalty units if line is short
        if total_req > total_cap + EPS:
            over = total_req - total_cap
            for it in sorted(items, key=lambda x: x[0]):
                if over <= EPS:
                    break
                cut = min(it[4], over)
                it[4] -= cut
                over -= cut
                if it[1] == "dem":
                    unmet_rows.append({"sku": it[2], "cfa": it[3], "kl": cut,
                                       "reason": f"line {g} capacity short",
                                       "penalty_cost": cut * penalty[it[2]]})
                else:
                    unmet_rows.append({"sku": it[2], "cfa": f"{it[3]} (hub SS)", "kl": cut,
                                       "reason": f"line {g} capacity short",
                                       "penalty_cost": cut * hub_short_cost})
            if over > EPS:
                warnings.append(f"Line {g}: could not shed enough demand; residual {over:.1f} kL")

        # step 4: exact transportation problem plants -> destinations
        dests = {}
        for it in items:
            if it[4] > EPS:
                key = ("C", it[3]) if it[1] == "dem" else ("H", it[3])
                dests[key] = dests.get(key, 0.0) + it[4]
        dlist = list(dests.keys())
        if dlist:
            n = 2 + len(PLANTS) + len(dlist)
            S, T = 0, n - 1
            mcf = MinCostFlow(n)
            pid = {p: 1 + i for i, p in enumerate(PLANTS)}
            did = {d: 1 + len(PLANTS) + i for i, d in enumerate(dlist)}
            for p in PLANTS:
                if cap[p] > EPS:
                    mcf.add_edge(S, pid[p], cap[p], 0.0)
            for dkey, q in dests.items():
                mcf.add_edge(did[dkey], T, q, 0.0)
                for p in PLANTS:
                    if cap[p] <= EPS:
                        continue
                    if dkey[0] == "C":
                        cost = best_path[(p, dkey[1])][0]
                    else:
                        cost = prod_cost[p] + tp.loc[p, dkey[1]]
                    mcf.add_edge(pid[p], did[dkey], float("inf"), cost)
            flows = mcf.solve(S, T)
            # plant->dest flows
            pd_flow = {}
            for (u, v), f in flows.items():
                pu = [p for p in PLANTS if pid[p] == u]
                dv = [d for d in dlist if did[d] == v]
                if pu and dv:
                    pd_flow[(pu[0], dv[0])] = pd_flow.get((pu[0], dv[0]), 0.0) + f

            # step 5: disaggregate to SKU level (contractual & high tier first)
            remain = {k: v for k, v in pd_flow.items()}
            order = sorted([it for it in items if it[4] > EPS],
                           key=lambda x: (not contractual.get(x[2], False),
                                          tier_of.get(x[2], "D"), -x[4]))
            for it in order:
                _, kind, s, dst, q = it
                dkey = ("C", dst) if kind == "dem" else ("H", dst)
                for p in sorted(PLANTS, key=lambda p: -remain.get((p, dkey), 0.0)):
                    if q <= EPS:
                        break
                    avail = remain.get((p, dkey), 0.0)
                    if avail <= EPS:
                        continue
                    take = min(avail, q)
                    remain[(p, dkey)] = avail - take
                    q -= take
                    prod[(s, p)] = prod.get((s, p), 0.0) + take
                    if kind == "dem":
                        h = best_path[(p, dst)][1]
                        ship[(s, p, h, dst)] = ship.get((s, p, h, dst), 0.0) + take
                    else:
                        ship[(s, p, dst, None)] = ship.get((s, p, dst, None), 0.0) + take
                        hub_top_served[(s, dst)] = hub_top_served.get((s, dst), 0.0) + take

        # step 6: batch rounding within the line (per SKU, relocatable residuals)
        used = {p: sum(v for (s2, p2), v in prod.items() if p2 == p and line_of[s2] == g)
                for p in PLANTS}
        slack = {p: float(plants.loc[p, g]) - used[p] for p in PLANTS}
        skus_in_g = sorted({s2 for (s2, p2) in prod if line_of[s2] == g},
                           key=lambda s: (not contractual.get(s, False), tier_of.get(s, "D"), s))
        for s in skus_in_g:
            resid_cuts = []      # (hub, cfa_or_None, qty) shipments displaced by flooring
            total_resid = 0.0
            for p in PLANTS:
                q = prod.get((s, p), 0.0)
                if q <= EPS:
                    continue
                lo = np.floor(q / BATCH + EPS) * BATCH
                r = q - lo
                if r <= EPS:
                    prod[(s, p)] = lo
                    continue
                prod[(s, p)] = lo
                slack[p] += r
                total_resid += r
                # displace r from this plant's shipments (top-ups first, they are cheapest to re-source)
                cand = [(k, v) for k, v in ship.items() if k[0] == s and k[1] == p and v > EPS]
                cand.sort(key=lambda kv: (kv[0][3] is not None))
                rr = r
                for k, v in cand:
                    if rr <= EPS:
                        break
                    cut = min(v, rr)
                    ship[k] = v - cut
                    rr -= cut
                    resid_cuts.append((k[2], k[3], cut))
                    if k[3] is None:
                        hub_top_served[(s, k[2])] = hub_top_served.get((s, k[2]), 0.0) - cut
            if total_resid <= EPS:
                continue
            # value of serving the residual vs cost of extra batches
            pen_avoided = sum(q * (float("inf") if (c is not None and contractual.get(s))
                                   else (penalty[s] if c is not None else hub_short_cost))
                              for (h, c, q) in resid_cuts)
            n_batches = int(np.ceil(total_resid / BATCH - EPS))
            placeable = []
            for p in sorted(PLANTS, key=lambda p: prod_cost[p] + min(tp.loc[p, h] for h in HUBS)):
                while slack[p] >= BATCH - EPS and len(placeable) < n_batches:
                    placeable.append(p)
                    slack[p] -= BATCH
            batch_cost = sum(BATCH * (prod_cost[p] + min(tp.loc[p, h] for h in HUBS))
                             for p in placeable)
            if placeable and pen_avoided >= batch_cost:
                # produce the batches, re-serve displaced shipments, park excess at hub
                cap_new = len(placeable) * BATCH
                for p in placeable:
                    prod[(s, p)] = prod.get((s, p), 0.0) + BATCH
                served = 0.0
                pi = 0
                room = BATCH
                for (h, c, q) in sorted(resid_cuts, key=lambda x: x[1] is None):
                    qq = q
                    while qq > EPS and pi < len(placeable):
                        p = placeable[pi]
                        take = min(qq, room)
                        if c is not None:
                            hb = best_path[(p, c)][1]
                            ship[(s, p, hb, c)] = ship.get((s, p, hb, c), 0.0) + take
                        else:
                            ship[(s, p, h, None)] = ship.get((s, p, h, None), 0.0) + take
                            hub_top_served[(s, h)] = hub_top_served.get((s, h), 0.0) + take
                        qq -= take
                        room -= take
                        served += take
                        if room <= EPS:
                            pi += 1
                            room = BATCH
                excess = cap_new - served
                if excess > EPS and pi < len(placeable):
                    p = placeable[pi]
                    hb = min(HUBS, key=lambda h: tp.loc[p, h])
                    ship[(s, p, hb, None)] = ship.get((s, p, hb, None), 0.0) + excess
            else:
                # not worth an extra batch (or no slack): return reserved batches
                for p in placeable:
                    slack[p] += BATCH
                dem_resid = [[h, c, q] for (h, c, q) in resid_cuts if c is not None]
                if contractual.get(s, False) and dem_resid:
                    # contractual demand must ship: displace the lowest-penalty
                    # non-contractual batch on this line to make room
                    need = sum(r[2] for r in dem_resid)
                    while need > EPS:
                        cands = [(penalty[s2], s2, p2) for (s2, p2), v in prod.items()
                                 if line_of[s2] == g and not contractual.get(s2)
                                 and v >= BATCH - EPS]
                        if not cands:
                            break
                        _, s2, p2 = min(cands)
                        prod[(s2, p2)] -= BATCH
                        rem = BATCH
                        c2 = [(k, v) for k, v in ship.items()
                              if k[0] == s2 and k[1] == p2 and v > EPS]
                        c2.sort(key=lambda kv: (kv[0][3] is not None,))
                        for k, v in c2:
                            if rem <= EPS:
                                break
                            cut = min(v, rem)
                            ship[k] = v - cut
                            rem -= cut
                            if k[3] is not None:
                                unmet_rows.append({"sku": s2, "cfa": k[3], "kl": cut,
                                                   "reason": "displaced by contractual batch",
                                                   "penalty_cost": cut * penalty[s2]})
                            else:
                                hub_top_served[(s2, k[2])] = hub_top_served.get((s2, k[2]), 0.0) - cut
                        prod[(s, p2)] = prod.get((s, p2), 0.0) + BATCH
                        room = BATCH
                        for r in dem_resid:
                            if room <= EPS or r[2] <= EPS:
                                continue
                            take = min(r[2], room)
                            hb = best_path[(p2, r[1])][1]
                            ship[(s, p2, hb, r[1])] = ship.get((s, p2, hb, r[1]), 0.0) + take
                            r[2] -= take
                            room -= take
                        if room > EPS:  # park leftover of the stolen batch at cheapest hub
                            hb = min(HUBS, key=lambda h: tp.loc[p2, h])
                            ship[(s, p2, hb, None)] = ship.get((s, p2, hb, None), 0.0) + room
                        need = sum(r[2] for r in dem_resid)
                for (h, c, q) in [(r[0], r[1], r[2]) for r in dem_resid if r[2] > EPS] if contractual.get(s, False)                         else [(h, c, q) for (h, c, q) in resid_cuts if c is not None]:
                    unmet_rows.append({"sku": s, "cfa": c, "kl": q,
                                       "reason": "batch rounding (uneconomic residual)"
                                                 if not contractual.get(s, False)
                                                 else "capacity infeasible (contractual)",
                                       "penalty_cost": q * penalty[s]})
                # top-up residuals simply remain as hub shortfall (costed later)

    # ---------- assemble outputs
    prod_rows = [{"sku": s, "plant": p, "line": line_of[s], "kl": v,
                  "batches": int(round(v / BATCH))}
                 for (s, p), v in prod.items() if v > EPS]
    production = pd.DataFrame(prod_rows)

    ph_rows = {}
    for (s, p, h, c), v in ship.items():
        if v > EPS:
            ph_rows[(p, h)] = ph_rows.get((p, h), 0.0) + v
    plant_hub = pd.DataFrame([{"plant": p, "hub": h, "kl": v} for (p, h), v in ph_rows.items()])

    for (s, p, h, c), v in ship.items():
        if v > EPS and c is not None:
            hub_cfa_rows.append({"sku": s, "hub": h, "cfa": c, "kl": v, "supply": "fresh"})
    hub_cfa = pd.DataFrame(hub_cfa_rows)
    if len(hub_cfa):
        hub_cfa = hub_cfa.groupby(["sku", "hub", "cfa", "supply"], as_index=False)["kl"].sum()

    unmet = pd.DataFrame(unmet_rows)
    if len(unmet):
        unmet = unmet.groupby(["sku", "cfa", "reason"], as_index=False)[["kl", "penalty_cost"]].sum()
        unmet = unmet.merge(skuinfo[["contractual"]].reset_index(), on="sku", how="left")
        unmet["tier"] = unmet["sku"].map(tier_of)

    # hub ending positions
    hub_rows = []
    for (s, h) in all_hub_idx:
        inflow_top = hub_top_served.get((s, h), 0.0)
        excess = sum(v for (s2, p2, h2, c2), v in ship.items()
                     if s2 == s and h2 == h and c2 is None) - inflow_top
        served_from_stock = sum(r["kl"] for r in hub_cfa_rows
                                if r["sku"] == s and r["hub"] == h and r["supply"] == "hub stock")
        end = ohub[(s, h)] - served_from_stock + inflow_top + max(excess, 0.0)
        hub_rows.append({"sku": s, "hub": h, "opening_kl": round(ohub[(s, h)], 2),
                         "ss_target_kl": round(hss[(s, h)], 2),
                         "ending_kl": round(end, 2),
                         "shortfall_kl": round(max(0.0, hss[(s, h)] - end), 2)})
    hub_position = pd.DataFrame(hub_rows)

    # ---------- costs
    c_prod = sum(v * prod_cost[p] for (s, p), v in prod.items())
    c_ph = sum(v * tp.loc[k[0], k[1]] for k, v in ph_rows.items())
    c_hc = 0.0
    for r in hub_cfa_rows:
        c_hc += r["kl"] * tc.loc[r["cfa"], r["hub"]]
    c_pen = float(unmet["penalty_cost"].sum()) if len(unmet) else 0.0
    c_hub_short = float(hub_position["shortfall_kl"].sum()) * hub_short_cost
    costs = {"production": c_prod, "transport_plant_hub": c_ph,
             "transport_hub_cfa": c_hc, "penalty_unmet": c_pen,
             "hub_ss_shortfall": c_hub_short,
             "total": c_prod + c_ph + c_hc + c_pen + c_hub_short}

    # ---------- KPIs
    tot_req = float((data.jan_forecast["qty"]).sum())
    tot_unmet = float(unmet["kl"].sum()) if len(unmet) else 0.0
    served = tot_req - tot_unmet
    util = {}
    for p in PLANTS:
        for g in LINE_GROUPS:
            capv = float(plants.loc[p, g])
            if capv > 0:
                usedv = sum(v for (s2, p2), v in prod.items()
                            if p2 == p and line_of[s2] == g)
                util[f"{p}|{g}"] = usedv / capv
    kpis = {"total_demand_kl": tot_req, "served_kl": served,
            "fill_rate": served / tot_req if tot_req else 1.0,
            "total_production_kl": sum(prod.values()),
            "utilization": util}
    return PlanResult(production, plant_hub, hub_cfa, unmet, hub_position,
                      costs, kpis, warnings)
