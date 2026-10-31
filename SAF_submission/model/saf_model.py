"""
SAF supply chain model for Kalimantan - Airbus Challenge #3 (Part A: network designer)

One file, four jobs:
  design    -> MILP: which depots/refineries to open, what size        (Stage 1, slow ~2 min)
  refine    -> move each facility to its exact cost-minimising point    (Weiszfeld, "anywhere")
  operate   -> LP: route material through FIXED facilities for a scenario (Stage 2, seconds)
  check     -> mass balance / capacity / demand sanity checks
  stress    -> knock out each refinery, then each district, record impact

Run:  pip install "pulp<3" pandas openpyxl scikit-learn
      python saf_model.py all  biomass.xlsx  Airport.dat.xlsx   -> writes results/*.json
"""
import json
import os
import sys
import copy

import numpy as np
import pandas as pd
import pulp
from sklearn.cluster import KMeans

# =====================================================================
# 1. INPUTS FROM THE BRIEF (do not change)
# =====================================================================
# capacity = biomass INTAKE per year (organiser update); var_opex = USD per tonne IN
DEPOT_SIZES = {"S": {"capex": 2.25e6, "cap_t": 150_000, "var_opex": 5.0},
               "L": {"capex": 4.5e6, "cap_t": 300_000, "var_opex": 4.0}}
REFINERY_SIZES = {"S": {"capex": 480e6, "cap_t": 500_000, "var_opex": 60.0},
                  "L": {"capex": 765e6, "cap_t": 1_087_000, "var_opex": 55.0}}
FIXED_OPEX_RATE = 0.05   # fixed OpEx = 5% of CapEx per year (organiser update)
# Organiser SAF demand, t/yr (total 1,200 kt). Juwata (TRK) is on an island: last leg is a barge at the same USD/t-km.
ORGANISER_DEMAND_T = {"PNK": 300_000, "PKN": 300_000, "BPN": 300_000, "TRK": 300_000}


# Logistics emission factors, kg CO2e per tonne-km (well-to-wheel). Indicative; editable in ASSUMPTIONS.
#  - small trucks (farm->depot, 3-9 t): GLEC Framework v3.0, rigid truck 3.5-7.5 t, average/mixed load, diesel = 0.242
#  - large trucks (18-32 t): GLEC default factors v2.0, Asia & Africa, rigid truck 12-20 t, average/mixed load = 0.1586
#  - barge (Juwata's last leg): GLEC default factors v2.0, inland waterway pushed convoy = 0.010
# Fossil jet fuel life-cycle baseline: 89 g CO2e/MJ (ICAO CORSIA methodology); jet fuel ~43 MJ/kg (our assumption).
EF_DEFAULT = {"farm_depot": 0.242, "depot_refinery": 0.1586, "refinery_airport": 0.1586, "barge": 0.010}
FOSSIL_JET_G_PER_MJ = 89.0
JET_MJ_PER_KG = 43.0


def annual_fixed(capex, years=20):
    """Annualised CapEx (straight line, no interest) + fixed OpEx, USD per year."""
    return capex / years + FIXED_OPEX_RATE * capex
RATE = {"farm_depot": 0.10, "depot_ref": 0.12, "ref_airport": 0.08}  # USD per tonne-km, same for both truck sizes
AIRPORTS = {"PNK": "Supadio", "PKN": "Iskandar", "BPN": "Sepinggan", "TRK": "Juwata"}

# =====================================================================
# 2. ASSUMPTIONS (not in the brief - every one goes on the assumptions slide)
# =====================================================================
ASSUMPTIONS = {
    "saf_yield": 0.20,             # t SAF per t biomass. PLACEHOLDER - confirm with organisers / literature
    "road_factor": float(os.environ.get("ROAD_FACTOR", 1.3)),            # road km = straight-line km x this. Person B replaces with real OSRM value
    "years": 20,                   # CapEx is one-off, transport is per year -> compare over 20 years
    "airport_demand_t": ORGANISER_DEMAND_T,   # organiser figures; set to None to use the total/weights below
    "total_saf_demand_t": float(os.environ.get("SAF_DEMAND_T", 0)) or None,    # None = all SAF the 6,800 kt can make (6,800 kt x yield)
    # Relative airport size used to split demand. ROUGH - Person A must confirm with organisers/real data
    "airport_weight": {"BPN": 7.0, "PNK": 4.0, "TRK": 1.0, "PKN": 0.65},
    "refinery_capacity_basis": "feedstock_in",   # capacities read as biomass input, consistent with depots
    "depot_mass_yield": 1.0,       # t leaving depot per t biomass in. Brief is silent -> 1.0 (no loss).
                                   # Drying/densifying would make this < 1 (e.g. 0.6) - ask organisers.
    "max_farm_depot_km": float(os.environ.get("MAX_FD_KM", 150)),      # farm can only ship to a depot within this road distance (design)
    "max_farm_depot_km_disrupted": 300,  # allow longer hauls when re-routing in a crisis
    "emission_factors": EF_DEFAULT,       # kg CO2e / t-km per leg (see sources above)
    "barge_km_to_TRK": 30.0,              # OUR assumption: last ~30 km to Tarakan island by barge
    "carbon_price_usd_per_t": 100.0,      # OUR assumption: used only by the "balanced" plan option
    "import_cost_per_t_saf": 3000, # if we can't supply an airport, assume it imports SAF at this USD/t
    "n_refinery_candidates": int(os.environ.get("N_REF_CAND", 15)),   # k-means centres used as starting refinery candidates
}


# =====================================================================
# Geometry helpers
# =====================================================================
def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi, dlmb = p2 - p1, np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def road_km(a, b, factor):
    a, b = np.atleast_2d(a), np.atleast_2d(b)
    return factor * haversine_km(a[:, None, 0], a[:, None, 1], b[None, :, 0], b[None, :, 1])


def weiszfeld(points, weights, start, iters=200):
    """Point minimising sum(weight_i * distance to point_i)  (the Weber point)."""
    if len(points) == 0 or weights.sum() <= 0:
        return start
    lat0 = np.mean(points[:, 0])
    kx = 111.32 * np.cos(np.radians(lat0))   # km per degree lon
    ky = 110.57                              # km per degree lat
    P = np.column_stack([points[:, 1] * kx, points[:, 0] * ky])
    cost = lambda q: (weights * np.linalg.norm(P - q, axis=1)).sum()
    x_start = np.array([start[1] * kx, start[0] * ky])
    x = (P * weights[:, None]).sum(0) / weights.sum()   # start at weighted centre (avoids getting stuck on a farm)
    for _ in range(iters):
        d = np.linalg.norm(P - x, axis=1)
        d = np.maximum(d, 1e-6)
        w = weights / d
        x_new = (P * w[:, None]).sum(0) / w.sum()
        if np.linalg.norm(x_new - x) < 1e-4:
            break
        x = x_new
    if cost(x_start) < cost(x):   # never accept a worse point than where we started
        x = x_start
    return np.array([x[1] / ky, x[0] / kx])


# =====================================================================
# Land check (organiser rule: facilities must be on land in Kalimantan)
# =====================================================================
_LAND = None


class _Ring:
    """A coastline polygon with a numpy point-in-polygon test (no matplotlib needed)."""

    def __init__(self, pts):
        self.vertices = np.asarray(pts, float)          # columns: lon, lat
        self.x0, self.y0 = self.vertices.min(0)
        self.x1, self.y1 = self.vertices.max(0)

    def contains_point(self, pt):
        x, y = pt
        if not (self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1):
            return False
        xi, yi = self.vertices[:, 0], self.vertices[:, 1]
        xj, yj = np.roll(xi, 1), np.roll(yi, 1)
        crosses = ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / np.where(yj - yi == 0, 1e-12, yj - yi) + xi)
        return bool(np.count_nonzero(crosses) % 2)


def land_rings():
    """Indonesian Borneo coastline (Natural Earth 1:10m), from kalimantan_land.json next to this file."""
    global _LAND
    if _LAND is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kalimantan_land.json")
        _LAND = [_Ring(r) for r in json.load(open(p))] if os.path.exists(p) else []
    return _LAND


def on_land(lat, lon):
    rings = land_rings()
    return True if not rings else any(r.contains_point((lon, lat)) for r in rings)


def snap_to_land(lat, lon, inland_km=2.0):
    """Nearest point on the coastline, nudged a little inland."""
    rings = land_rings()
    if not rings or on_land(lat, lon):
        return lat, lon
    best = None
    for r in rings:
        v = r.vertices
        d = haversine_km(lat, lon, v[:, 1], v[:, 0])
        i = int(np.argmin(d))
        if best is None or d[i] < best[0]:
            best = (d[i], v[i, 1], v[i, 0])
    _, blat, blon = best
    # walk from the coast point away from the original (sea) point until inside
    dlat, dlon = blat - lat, blon - lon
    norm = max(np.hypot(dlat, dlon), 1e-9)
    for k in range(1, 40):
        step = k * inland_km / 111.0 / 2
        tlat, tlon = blat + dlat / norm * step, blon + dlon / norm * step
        if on_land(tlat, tlon):
            return float(tlat), float(tlon)
    return float(blat), float(blon)


# =====================================================================
# Data loading
# =====================================================================
def load_data(biomass_path, airport_path):
    bio = pd.read_csv(biomass_path) if biomass_path.endswith(".csv") else pd.read_excel(biomass_path)
    ap = pd.read_excel(airport_path, header=None) if airport_path.endswith((".xlsx", ".xls")) \
        else pd.read_csv(airport_path, header=None)
    ap = ap.iloc[:, [1, 4, 6, 7]]
    ap.columns = ["name", "iata", "lat", "lon"]
    ap = ap[ap.iata.isin(AIRPORTS)].set_index("iata").loc[list(AIRPORTS)].reset_index()
    ap["short"] = ap.iata.map(AIRPORTS)
    return bio.reset_index(drop=True), ap


def airport_demand(bio, ap, A):
    if A.get("airport_demand_t"):
        return {c: float(A["airport_demand_t"][c]) for c in ap.iata}
    total = A["total_saf_demand_t"] or bio.production_tonnes.sum() * A["saf_yield"]
    w = A["airport_weight"]
    s = sum(w[c] for c in ap.iata)
    return {c: total * w[c] / s for c in ap.iata}


# =====================================================================
# STAGE 1: DESIGN (MILP) - which facilities, what size
# =====================================================================
def design(bio, ap, A, time_limit=180):
    farms = bio[["latitude", "longitude"]].to_numpy()
    supply = bio.production_tonnes.to_numpy(float)
    demand = airport_demand(bio, ap, A)
    air = ap[["lat", "lon"]].to_numpy()

    dep_cand = farms.copy()  # starting candidates only - refine() moves them freely afterwards
    km = KMeans(n_clusters=A["n_refinery_candidates"], n_init=10, random_state=0).fit(farms, sample_weight=supply)
    ref_cand = np.vstack([km.cluster_centers_, air])

    f = A["road_factor"]
    d_fd, d_dr, d_ra = road_km(farms, dep_cand, f), road_km(dep_cand, ref_cand, f), road_km(ref_cand, air, f)
    F, D, R, Aa = range(len(farms)), range(len(dep_cand)), range(len(ref_cand)), range(len(air))
    fd = [(i, j) for i in F for j in D if d_fd[i, j] <= A["max_farm_depot_km"]]

    m = pulp.LpProblem("design", pulp.LpMinimize)
    yd = {(j, s): pulp.LpVariable(f"yd_{j}_{s}", cat="Binary") for j in D for s in DEPOT_SIZES}
    yr = {(k, s): pulp.LpVariable(f"yr_{k}_{s}", cat="Binary") for k in R for s in REFINERY_SIZES}
    x = {a: pulp.LpVariable(f"x_{a[0]}_{a[1]}", lowBound=0) for a in fd}
    z = {(j, k): pulp.LpVariable(f"z_{j}_{k}", lowBound=0) for j in D for k in R}
    w = {(k, a): pulp.LpVariable(f"w_{k}_{a}", lowBound=0) for k in R for a in Aa}
    qd = {(j, s): pulp.LpVariable(f"qd_{j}_{s}", lowBound=0) for j in D for s in DEPOT_SIZES}      # intake by size
    qr = {(k, s): pulp.LpVariable(f"qr_{k}_{s}", lowBound=0) for k in R for s in REFINERY_SIZES}

    Y = A["years"]
    fixed = pulp.lpSum(annual_fixed(DEPOT_SIZES[s]["capex"], Y) * yd[j, s] for j in D for s in DEPOT_SIZES) + \
        pulp.lpSum(annual_fixed(REFINERY_SIZES[s]["capex"], Y) * yr[k, s] for k in R for s in REFINERY_SIZES)
    var_opex = pulp.lpSum(DEPOT_SIZES[s]["var_opex"] * qd[j, s] for j in D for s in DEPOT_SIZES) + \
        pulp.lpSum(REFINERY_SIZES[s]["var_opex"] * qr[k, s] for k in R for s in REFINERY_SIZES)
    transport = pulp.lpSum(RATE["farm_depot"] * d_fd[i, j] * x[i, j] for (i, j) in fd) + \
        pulp.lpSum(RATE["depot_ref"] * d_dr[j, k] * z[j, k] for j in D for k in R) + \
        pulp.lpSum(RATE["ref_airport"] * d_ra[k, a] * w[k, a] for k in R for a in Aa)
    m += fixed + var_opex + transport        # USD per year (organiser objective)

    into = {j: [] for j in D}
    outof = {i: [] for i in F}
    for (i, j) in fd:
        into[j].append(x[i, j])
        outof[i].append(x[i, j])
    for i in F:
        m += pulp.lpSum(outof[i]) <= supply[i]
    for j in D:
        m += pulp.lpSum(yd[j, s] for s in DEPOT_SIZES) <= 1
        m += pulp.lpSum(into[j]) == pulp.lpSum(qd[j, s] for s in DEPOT_SIZES)
        for s in DEPOT_SIZES:
            m += qd[j, s] <= DEPOT_SIZES[s]["cap_t"] * yd[j, s]
        m += pulp.lpSum(z[j, k] for k in R) == A["depot_mass_yield"] * pulp.lpSum(into[j])
    for k in R:
        feed = pulp.lpSum(z[j, k] for j in D)
        m += pulp.lpSum(yr[k, s] for s in REFINERY_SIZES) <= 1
        m += feed == pulp.lpSum(qr[k, s] for s in REFINERY_SIZES)
        for s in REFINERY_SIZES:
            m += qr[k, s] <= REFINERY_SIZES[s]["cap_t"] * yr[k, s]
        m += pulp.lpSum(w[k, a] for a in Aa) == A["saf_yield"] * feed
    for a, code in zip(Aa, ap.iata):
        m += pulp.lpSum(w[k, a] for k in R) >= demand[code]

    m.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit, gapRel=0.01))

    depots = [{"id": f"D{n+1:02d}", "lat": float(dep_cand[j, 0]), "lon": float(dep_cand[j, 1]),
               "size": s, "cap_t": DEPOT_SIZES[s]["cap_t"], "capex": DEPOT_SIZES[s]["capex"]}
              for n, ((j, s), v) in enumerate((k, v) for k, v in yd.items() if v.value() > 0.5)]
    refs = [{"id": f"R{n+1}", "lat": float(ref_cand[k, 0]), "lon": float(ref_cand[k, 1]),
             "size": s, "cap_t": REFINERY_SIZES[s]["cap_t"], "capex": REFINERY_SIZES[s]["capex"]}
            for n, ((k, s), v) in enumerate((k, v) for k, v in yr.items() if v.value() > 0.5)]
    return {"solver_status": pulp.LpStatus[m.status], "depots": depots, "refineries": refs}


# =====================================================================
# STAGE 2: OPERATE (LP) - route material through FIXED facilities
# =====================================================================
BASE_SCENARIO = {"name": "base"}


def operate(bio, ap, A, network, scenario=BASE_SCENARIO, design_mode=False):
    """
    scenario keys (all optional):
      farms_offline: [location_id, ...]          e.g. flooded farms
      districts_offline: ["Ketapang", ...]       every farm in that district produces 0
      farm_supply_factor: {location_id: 0.5}     e.g. drought halves output
      district_supply_factor: {"Paser": 0.5}
      depots_offline / refineries_offline: ["D03", "R2"]
      refinery_capacity_factor: {"R2": 0.5}      e.g. partial outage
      demand_factor: {"BPN": 1.3}                e.g. holiday peak +30%
      blocked_links: [["D03","R2"], ["R1","TRK"]]  flooded road between two facilities
    """
    sc = scenario
    farms = bio[["latitude", "longitude"]].to_numpy()
    supply = bio.production_tonnes.to_numpy(float).copy()
    # localised events: {"lat","lon","radius_km","severity" 0-1,"weeks","hits_facilities"}; several may overlap
    events = list(sc.get("events") or []) + ([sc["event"]] if sc.get("event") else [])
    ev_mult = np.ones(len(farms))
    for ev in events:
        inside = haversine_km(ev["lat"], ev["lon"], farms[:, 0], farms[:, 1]) <= ev["radius_km"]
        ev_mult[inside] *= 1 - ev["severity"]
    ev_farm = ev_mult < 1
    for idx, row in bio.iterrows():
        if row.location_id in sc.get("farms_offline", []) or row.district in sc.get("districts_offline", []):
            supply[idx] = 0
        supply[idx] *= sc.get("farm_supply_factor", {}).get(row.location_id, 1.0)
        supply[idx] *= sc.get("district_supply_factor", {}).get(row.district, 1.0)
        supply[idx] *= ev_mult[idx]
    demand = airport_demand(bio, ap, A)
    demand = {c: v * sc.get("demand_factor", {}).get(c, 1.0) for c, v in demand.items()}

    dep, ref = network["depots"], network["refineries"]
    dxy = np.array([[d["lat"], d["lon"]] for d in dep])
    rxy = np.array([[r["lat"], r["lon"]] for r in ref])
    air = ap[["lat", "lon"]].to_numpy()
    f = A["road_factor"]
    d_fd, d_dr, d_ra = road_km(farms, dxy, f), road_km(dxy, rxy, f), road_km(rxy, air, f)
    radius = A["max_farm_depot_km"] if design_mode else A["max_farm_depot_km_disrupted"]
    blocked = {tuple(b) for b in sc.get("blocked_links", [])}

    dcap = [0 if d["id"] in sc.get("depots_offline", []) else d["cap_t"] for d in dep]
    rcap = [0 if r["id"] in sc.get("refineries_offline", [])
            else r["cap_t"] * sc.get("refinery_capacity_factor", {}).get(r["id"], 1.0) for r in ref]
    ev_fac = []
    for ev in events:
        if not ev.get("hits_facilities"):
            continue
        for lst, cap in ((dep, dcap), (ref, rcap)):
            for n_, fac in enumerate(lst):
                if haversine_km(ev["lat"], ev["lon"], fac["lat"], fac["lon"]) <= ev["radius_km"]:
                    cap[n_] *= 1 - ev["severity"]
                    if fac["id"] not in ev_fac:
                        ev_fac.append(fac["id"])
    EF = A.get("emission_factors", EF_DEFAULT)
    bkm = A.get("barge_km_to_TRK", 30.0)

    def ef_ra(k, a):   # kg CO2e per t-km on refinery->airport, with barge for Juwata's last leg
        km = d_ra[k, a]
        if ap.iata[a] != "TRK" or km <= 0:
            return EF["refinery_airport"]
        b = min(bkm, km)
        return (EF["refinery_airport"] * (km - b) + EF["barge"] * b) / km

    F, D, R, Aa = range(len(farms)), range(len(dep)), range(len(ref)), range(len(air))
    fd = [(i, j) for i in F for j in D if d_fd[i, j] <= radius and supply[i] > 0]
    dr = [(j, k) for j in D for k in R if (dep[j]["id"], ref[k]["id"]) not in blocked]
    cut_off = set(sc.get("airports_unreachable", []))   # e.g. Tarakan barge/port closed
    ra = [(k, a) for k in R for a in Aa if (ref[k]["id"], ap.iata[a]) not in blocked and ap.iata[a] not in cut_off]

    m = pulp.LpProblem("operate", pulp.LpMinimize)
    x = {a: pulp.LpVariable(f"x_{a[0]}_{a[1]}", lowBound=0) for a in fd}
    z = {a: pulp.LpVariable(f"z_{a[0]}_{a[1]}", lowBound=0) for a in dr}
    w = {a: pulp.LpVariable(f"w_{a[0]}_{a[1]}", lowBound=0) for a in ra}
    short = {a: pulp.LpVariable(f"short_{a}", lowBound=0) for a in Aa}

    tf = sc.get("transport_cost_factor", 1.0)   # e.g. 1.3 = fuel price spike adds 30% to haulage
    transport = tf * (pulp.lpSum(RATE["farm_depot"] * d_fd[i, j] * v for (i, j), v in x.items()) +
                      pulp.lpSum(RATE["depot_ref"] * d_dr[j, k] * v for (j, k), v in z.items()) +
                      pulp.lpSum(RATE["ref_airport"] * d_ra[k, a] * v for (k, a), v in w.items()))
    imports = pulp.lpSum(A["import_cost_per_t_saf"] * short[a] for a in Aa)
    dep_in = {j: pulp.lpSum(v for (i, jj), v in x.items() if jj == j) for j in D}
    ref_in = {k: pulp.lpSum(v for (j, kk), v in z.items() if kk == k) for k in R}
    var_opex = pulp.lpSum(DEPOT_SIZES[dep[j]["size"]]["var_opex"] * dep_in[j] for j in D) + \
        pulp.lpSum(REFINERY_SIZES[ref[k]["size"]]["var_opex"] * ref_in[k] for k in R)
    co2 = (pulp.lpSum(EF["farm_depot"] * d_fd[i, j] * v for (i, j), v in x.items()) +
           pulp.lpSum(EF["depot_refinery"] * d_dr[j, k] * v for (j, k), v in z.items()) +
           pulp.lpSum(ef_ra(k, a) * d_ra[k, a] * v for (k, a), v in w.items())) / 1000.0   # t CO2e / yr
    mode = sc.get("objective", "cost")
    BIG = 1e6   # USD per t SAF short: forces "deliver first" in every mode
    if mode == "carbon":          # lowest emissions, still delivering as much SAF as possible
        m += BIG * pulp.lpSum(short.values()) + 1000 * co2 + 1e-3 * (transport + var_opex)
    elif mode == "balanced":      # cost + a carbon price
        m += transport + var_opex + imports + A.get("carbon_price_usd_per_t", 100.0) * co2
    elif mode == "fair":          # spread any shortfall in proportion to each airport's demand
        zfair = pulp.LpVariable("max_short_share", lowBound=0)
        for a, code in zip(Aa, ap.iata):
            m += short[a] <= zfair * demand[code]
        m += 1e9 * zfair + transport + var_opex + imports
    else:                         # "cost": cheapest (default)
        m += transport + var_opex + imports

    for i in F:
        m += pulp.lpSum(v for (ii, j), v in x.items() if ii == i) <= supply[i]
    for j in D:
        inflow = pulp.lpSum(v for (i, jj), v in x.items() if jj == j)
        m += inflow <= dcap[j]
        m += pulp.lpSum(v for (jj, k), v in z.items() if jj == j) == A["depot_mass_yield"] * inflow
    for k in R:
        feed = pulp.lpSum(v for (j, kk), v in z.items() if kk == k)
        m += feed <= rcap[k]
        m += pulp.lpSum(v for (kk, a), v in w.items() if kk == k) == A["saf_yield"] * feed
    for a, code in zip(Aa, ap.iata):
        m += pulp.lpSum(v for (k, aa), v in w.items() if aa == a) + short[a] >= demand[code]

    m.solve(pulp.PULP_CBC_CMD(msg=False))
    val = lambda v: max(0.0, v.value() or 0.0)

    flows = {
        "farm_depot": [{"from": bio.location_id[i], "to": dep[j]["id"], "t": val(v), "km": float(d_fd[i, j]),
                        "co2_t": val(v) * d_fd[i, j] * EF["farm_depot"] / 1000}
                       for (i, j), v in x.items() if val(v) > 1],
        "depot_refinery": [{"from": dep[j]["id"], "to": ref[k]["id"], "t": val(v), "km": float(d_dr[j, k]),
                            "co2_t": val(v) * d_dr[j, k] * EF["depot_refinery"] / 1000}
                           for (j, k), v in z.items() if val(v) > 1],
        "refinery_airport": [{"from": ref[k]["id"], "to": ap.iata[a], "t": val(v), "km": float(d_ra[k, a]),
                              "co2_t": val(v) * d_ra[k, a] * ef_ra(k, a) / 1000,
                              "mode": "truck + barge" if ap.iata[a] == "TRK" else "truck"}
                             for (k, a), v in w.items() if val(v) > 1],
    }
    co2_tot = sum(f["co2_t"] for leg in flows.values() for f in leg)
    saf_t = sum(f["t"] for f in flows["refinery_airport"])
    g_per_mj = co2_tot * 1e6 / (saf_t * 1000 * JET_MJ_PER_KG) if saf_t else 0.0
    tkm = sum(f["t"] * f["km"] for leg in flows.values() for f in leg)
    emissions = {"total_t_per_year": co2_tot,
                 "by_leg_t": {k: sum(f["co2_t"] for f in v) for k, v in flows.items()},
                 "kg_per_t_saf": co2_tot * 1000 / saf_t if saf_t else 0.0,
                 "g_per_MJ": g_per_mj, "pct_of_fossil_jet_baseline": 100 * g_per_mj / FOSSIL_JET_G_PER_MJ,
                 "tonne_km_per_year": tkm}
    capex = sum(d["capex"] for d in dep) + sum(r["capex"] for r in ref)
    tr = pulp.value(transport) or 0.0
    vo = pulp.value(var_opex) or 0.0
    im = pulp.value(imports) or 0.0
    Y = A["years"]
    return {
        "scenario": scenario, "solver_status": pulp.LpStatus[m.status],
        "assumptions": {k: v for k, v in A.items()},
        "network": network, "flows": flows,
        "supply_t": dict(zip(bio.location_id, supply.tolist())),
        "demand_t": demand,
        "shortfall_t": {ap.iata[a]: val(short[a]) for a in Aa},
        "cost": {"capex": capex, "annualised_capex": capex / Y, "fixed_opex_per_year": FIXED_OPEX_RATE * capex,
                 "variable_opex_per_year": vo, "transport_per_year": tr, "imports_per_year": im,
                 "total_annual": capex / Y + FIXED_OPEX_RATE * capex + vo + tr + im,
                 "total_20yr": Y * (capex / Y + FIXED_OPEX_RATE * capex + vo + tr + im)},
        "airports": ap[["iata", "short", "lat", "lon"]].to_dict("records"),
        "emissions": emissions, "objective": mode,
        "events": events,
        "event": ({"farms_hit": [bio.location_id[i] for i in range(len(farms)) if ev_farm[i]],
                   "facilities_hit": ev_fac} if events else None),
    }


# =====================================================================
# PLAN OPTIONS: same facilities, different priorities -> operator chooses
# =====================================================================
PLAN_MODES = {
    "cost": "Cheapest",
    "carbon": "Lowest carbon",
    "balanced": "Balanced (cost + carbon price)",
    "fair": "Fair shares (spread any shortfall)",
}


def plan_options(bio, ap, A, network, scenario):
    out = []
    disrupted = any(k for k in scenario if k not in ("name", "objective"))
    for mode, label in PLAN_MODES.items():
        r = operate(bio, ap, A, network, {**scenario, "objective": mode}, design_mode=not disrupted)
        c, e = r["cost"], r["emissions"]
        dem = sum(r["demand_t"].values())
        short = sum(r["shortfall_t"].values())
        km = [f["km"] for f in r["flows"]["refinery_airport"]]
        out.append({"mode": mode, "plan": label, "result": r,
                    "operating_cost_musd": (c["transport_per_year"] + c["variable_opex_per_year"]) / 1e6,
                    "co2_kt": e["total_t_per_year"] / 1e3, "tonne_km_bn": e["tonne_km_per_year"] / 1e9,
                    "demand_met_pct": 100 * (1 - short / dem) if dem else 100.0,
                    "short_kt": {k: v / 1e3 for k, v in r["shortfall_t"].items() if v > 1},
                    "worst_airport_met_pct": min(100 * (1 - r["shortfall_t"][k] / r["demand_t"][k]) for k in r["demand_t"]),
                    "longest_saf_haul_km": max(km) if km else 0.0})
    # rule-based pros / cons (no LLM): compare each plan with the others
    best = lambda key, lo=True: (min if lo else max)(p[key] for p in out)
    for p in out:
        pros, cons = [], []
        for key, lo, good, unit in [("operating_cost_musd", True, "lowest operating cost", "$M/yr"),
                                    ("co2_kt", True, "lowest CO2e", "kt/yr"),
                                    ("demand_met_pct", False, "most SAF delivered", "%"),
                                    ("worst_airport_met_pct", False, "no airport left far behind", "% at worst airport"),
                                    ("longest_saf_haul_km", True, "shortest longest haul", "km")]:
            b = best(key, lo)
            gap = (p[key] - b) if lo else (b - p[key])
            if abs(gap) < 1e-6 or (b and abs(gap) / abs(b) < 0.005):
                pros.append(good)
            elif b and abs(gap) / abs(b) > 0.02:
                cons.append(f"{abs(gap):,.1f} {unit} worse than best")
        # absolute checks, so a plan is never praised just for being "least bad"
        if p["worst_airport_met_pct"] < 90 and "no airport left far behind" in pros:
            pros.remove("no airport left far behind")
        if p["demand_met_pct"] < 99.95 and "most SAF delivered" in pros:
            pros.remove("most SAF delivered")
            pros.append("delivers as much SAF as any option")
        for code, kt in p["short_kt"].items():
            dem = p["result"]["demand_t"][code] / 1e3
            cons.append(f"{code} gets {100 * (1 - kt / dem):.0f}% of demand ({kt:,.0f} kt/yr short)")
        p["pros"], p["cons"] = pros, cons
    return out


# =====================================================================
# REFINE: move every facility to its exact cost-minimising coordinates
# =====================================================================
def refine(bio, ap, A, network, rounds=8):
    """Alternate: (1) route with current positions (2) step each facility towards its Weber point.
    A step is only kept if total cost falls (step halving), so refinement can never make things worse."""
    net = copy.deepcopy(network)
    for d in net["depots"] + net["refineries"]:
        d["start_lat"], d["start_lon"] = d["lat"], d["lon"]
    farm_xy = {r.location_id: (r.latitude, r.longitude) for r in bio.itertuples()}
    air_xy = {r.iata: (r.lat, r.lon) for r in ap.itertuples()}
    res = operate(bio, ap, A, net, design_mode=True)
    history = [res["cost"]["total_annual"]]
    for _ in range(rounds):
        pos = {f["id"]: (f["lat"], f["lon"]) for f in net["depots"] + net["refineries"]}
        target = {}
        for fac in net["depots"] + net["refineries"]:
            pts, wts = [], []
            for kind, rate in [("farm_depot", RATE["farm_depot"]), ("depot_refinery", RATE["depot_ref"]),
                               ("refinery_airport", RATE["ref_airport"])]:
                for fl in res["flows"][kind]:
                    if fl["to"] == fac["id"]:
                        pts.append(farm_xy.get(fl["from"]) or pos[fl["from"]]); wts.append(rate * fl["t"])
                    elif fl["from"] == fac["id"]:
                        pts.append(air_xy.get(fl["to"]) or pos[fl["to"]]); wts.append(rate * fl["t"])
            if pts:
                target[fac["id"]] = weiszfeld(np.array(pts), np.array(wts), np.array(pos[fac["id"]]))
        improved = False
        for step in (1.0, 0.5, 0.25, 0.1):
            trial = copy.deepcopy(net)
            for fac in trial["depots"] + trial["refineries"]:
                if fac["id"] in target:
                    t = target[fac["id"]]
                    fac["lat"] = float(fac["lat"] + step * (t[0] - fac["lat"]))
                    fac["lon"] = float(fac["lon"] + step * (t[1] - fac["lon"]))
            r = operate(bio, ap, A, trial, design_mode=True)
            if r["cost"]["total_annual"] < history[-1] * (1 - 1e-5):
                net, res, improved = trial, r, True
                history.append(r["cost"]["total_annual"])
                break
        if not improved:
            break
    # Organiser rule: facilities must be on land. Snap any offshore facility to the nearest land point.
    moved = []
    for fac in net["depots"] + net["refineries"]:
        if not on_land(fac["lat"], fac["lon"]):
            nl = snap_to_land(fac["lat"], fac["lon"])
            moved.append({"id": fac["id"], "km": round(float(haversine_km(fac["lat"], fac["lon"], nl[0], nl[1])), 1)})
            fac["lat"], fac["lon"] = nl
    if moved:
        res = operate(bio, ap, A, net, design_mode=True)
        history.append(res["cost"]["total_annual"])
    res["snapped_to_land"] = moved
    # Drop any facility that ended up with no throughput (rerouting made it redundant) - saves its CapEx
    used = {f["to"] for k in ("farm_depot", "depot_refinery") for f in res["flows"][k]}
    unused = [f["id"] for f in net["depots"] + net["refineries"] if f["id"] not in used]
    if unused:
        net["depots"] = [d for d in net["depots"] if d["id"] in used]
        net["refineries"] = [r for r in net["refineries"] if r["id"] in used]
        res = operate(bio, ap, A, net, design_mode=True)
        history.append(res["cost"]["total_annual"])
    res["removed_unused_facilities"] = unused
    # Right-size: a large depot handling <= 150 kt is cheaper as a small one (fixed $225k vs $450k/yr)
    intake = {}
    for f_ in res["flows"]["farm_depot"]:
        intake[f_["to"]] = intake.get(f_["to"], 0) + f_["t"]
    downsized = []
    for d in net["depots"]:
        if d["size"] == "L" and intake.get(d["id"], 0) <= DEPOT_SIZES["S"]["cap_t"] + 1e-6:
            d.update(size="S", cap_t=DEPOT_SIZES["S"]["cap_t"], capex=DEPOT_SIZES["S"]["capex"])
            downsized.append(d["id"])
    if downsized:
        new = operate(bio, ap, A, net, design_mode=True)
        if new["cost"]["total_annual"] <= res["cost"]["total_annual"] and sum(new["shortfall_t"].values()) < 1:
            res = new
            history.append(res["cost"]["total_annual"])
            res["removed_unused_facilities"] = unused
    res["downsized_depots"] = downsized
    res["refine_history_total_annual"] = history
    return res


# =====================================================================
# Facility justification (why here?)
# =====================================================================
def justify(bio, res):
    farm_by_id = bio.set_index("location_id")
    out = []
    for fac in res["network"]["depots"] + res["network"]["refineries"]:
        fid = fac["id"]
        inflow = [f for k in res["flows"] for f in res["flows"][k] if f["to"] == fid]
        outflow = [f for k in res["flows"] for f in res["flows"][k] if f["from"] == fid]
        t_in = sum(f["t"] for f in inflow)
        near = haversine_km(fac["lat"], fac["lon"], farm_by_id.latitude.values, farm_by_id.longitude.values)
        nf = farm_by_id.iloc[int(np.argmin(near))]
        moved = float(haversine_km(fac.get("start_lat", fac["lat"]), fac.get("start_lon", fac["lon"]),
                                   fac["lat"], fac["lon"]))
        out.append({
            "id": fid, "type": "depot" if fid.startswith("D") else "refinery", "size": fac["size"],
            "lat": round(fac["lat"], 4), "lon": round(fac["lon"], 4),
            "near": f"{nf.district}, {nf.province}", "km_to_nearest_farm": round(float(near.min()), 1),
            "inflow_kt": round(t_in / 1e3, 1), "utilisation_pct": round(100 * t_in / fac["cap_t"], 1),
            "suppliers": len(inflow), "customers": sorted({f["to"] for f in outflow}),
            "avg_inbound_km": round(sum(f["t"] * f["km"] for f in inflow) / t_in, 1) if t_in else None,
            "moved_by_refinement_km": round(moved, 1),
            "why": (f"Weber point of its {len(inflow)} suppliers and {len({f['to'] for f in outflow})} customers: "
                    f"minimises tonne-km cost for {t_in/1e3:.0f} kt/yr throughput"),
        })
    return out


# =====================================================================
# SANITY CHECKS (like a mass balance)
# =====================================================================
def check(res, tol=1.0):
    A, fl = res["assumptions"], res["flows"]
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "pass": bool(ok), "detail": detail})

    add("solver finished", res["solver_status"] == "Optimal", res["solver_status"])
    sent = {}
    for f in fl["farm_depot"]:
        sent[f["from"]] = sent.get(f["from"], 0) + f["t"]
    over = [k for k, v in sent.items() if v > res["supply_t"][k] + tol]
    add("no farm ships more than it produces", not over, f"violations: {over}")
    for d in res["network"]["depots"]:
        tin = sum(f["t"] for f in fl["farm_depot"] if f["to"] == d["id"])
        tout = sum(f["t"] for f in fl["depot_refinery"] if f["from"] == d["id"])
        if abs(A["depot_mass_yield"] * tin - tout) > tol or tin > d["cap_t"] + tol:
            add(f"depot {d['id']} balance & capacity", False, f"in {tin:.0f}, out {tout:.0f}, cap {d['cap_t']}")
    add("all depots: out = depot yield x in, within capacity", all(c["pass"] for c in checks if c["check"].startswith("depot ")),
        f"{len(res['network']['depots'])} depots checked")
    for r in res["network"]["refineries"]:
        feed = sum(f["t"] for f in fl["depot_refinery"] if f["to"] == r["id"])
        saf = sum(f["t"] for f in fl["refinery_airport"] if f["from"] == r["id"])
        if abs(saf - A["saf_yield"] * feed) > tol or feed > r["cap_t"] + tol:
            add(f"refinery {r['id']} yield & capacity", False, f"feed {feed:.0f}, SAF {saf:.0f}")
    add("all refineries: SAF out = yield x feed, within capacity",
        all(c["pass"] for c in checks if c["check"].startswith("refinery ")), f"yield {A['saf_yield']}")
    for code, dem in res["demand_t"].items():
        got = sum(f["t"] for f in fl["refinery_airport"] if f["to"] == code)
        add(f"{code} demand met (delivered + imported)", got + res["shortfall_t"][code] >= dem - tol,
            f"demand {dem/1e3:.0f} kt, delivered {got/1e3:.0f} kt, imported {res['shortfall_t'][code]/1e3:.0f} kt")
    offshore = [f["id"] for f in res["network"]["depots"] + res["network"]["refineries"] if not on_land(f["lat"], f["lon"])]
    add("all facilities on land in Kalimantan", not offshore, f"offshore: {offshore}" if offshore else "all on land")
    bio_in = sum(f["t"] for f in fl["farm_depot"])
    saf_out = sum(f["t"] for f in fl["refinery_airport"])
    add("overall mass balance: SAF = yields x biomass",
        abs(saf_out - A["saf_yield"] * A["depot_mass_yield"] * bio_in) < tol * 10,
        f"biomass {bio_in/1e3:.0f} kt -> SAF {saf_out/1e3:.0f} kt")
    return checks


# =====================================================================
# STRESS TEST
# =====================================================================
def stress(bio, ap, A, network, base_res):
    base_op = base_res["cost"]["transport_per_year"] + base_res["cost"]["variable_opex_per_year"]
    rows = []
    tests = [("refinery", r["id"], {"name": f"{r['id']} offline", "refineries_offline": [r["id"]]})
             for r in network["refineries"]]
    tests += [("district flood", d, {"name": f"{d} flooded", "districts_offline": [d]})
              for d in sorted(bio.district.unique())]
    for kind, what, sc in tests:
        r = operate(bio, ap, A, network, sc)
        rows.append({
            "test": kind, "what": what,
            "extra_operating_cost_musd_per_yr": round((r["cost"]["transport_per_year"] + r["cost"]["variable_opex_per_year"] - base_op) / 1e6, 1),
            "saf_short_kt": {k: round(v / 1e3, 1) for k, v in r["shortfall_t"].items() if v > 1},
            "import_cost_musd_per_yr": round(r["cost"]["imports_per_year"] / 1e6, 1),
            "total_short_kt": round(sum(r["shortfall_t"].values()) / 1e3, 1),
        })
    return rows


# =====================================================================
# CLI
# =====================================================================
def save(obj, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=1, default=float)


if __name__ == "__main__":
    cmd, bio_path, ap_path = sys.argv[1], sys.argv[2], sys.argv[3]
    out = sys.argv[4] if len(sys.argv) > 4 else "results"
    bio, ap = load_data(bio_path, ap_path)
    A = ASSUMPTIONS

    if cmd in ("all", "design", "refine"):
        if cmd == "refine" and os.path.exists(f"{out}/network_initial.json"):
            net = json.load(open(f"{out}/network_initial.json"))   # skip the slow MILP
        else:
            print("Stage 1: designing network (MILP, ~2-3 min)...")
            net = design(bio, ap, A)
            save(net, f"{out}/network_initial.json")
        print("  solver:", net["solver_status"], "| depots", len(net["depots"]), "| refineries", len(net["refineries"]))
        print("Refining coordinates (facilities can sit anywhere)...")
        base = refine(bio, ap, A, net)
        h = base["refine_history_total_annual"]
        print(f"  annual cost ${h[0]/1e6:.1f}M -> ${h[-1]/1e6:.1f}M/yr after refinement")
        c = base["cost"]
        print(f"  breakdown/yr: CapEx/20 ${c['annualised_capex']/1e6:.1f}M | fixed OpEx ${c['fixed_opex_per_year']/1e6:.1f}M | "
              f"variable OpEx ${c['variable_opex_per_year']/1e6:.1f}M | transport ${c['transport_per_year']/1e6:.1f}M")
        base["justification"] = justify(bio, base)
        base["checks"] = check(base)
        save(base, f"{out}/base.json")
        save(base["network"], f"{out}/network.json")
        for c in base["checks"]:
            print("  ", "PASS" if c["pass"] else "FAIL", c["check"], "-", c["detail"])

    if cmd in ("all", "refine", "scenarios", "stress"):
        net = json.load(open(f"{out}/network.json"))
        base = json.load(open(f"{out}/base.json"))
        if cmd in ("all", "refine", "scenarios"):
            examples = [
                {"name": "Ketapang flooded", "districts_offline": ["Ketapang"]},
                {"name": "Drought: Paser output -50%", "district_supply_factor": {"Paser": 0.5}},
                {"name": "Holiday peak: all airports +30%", "demand_factor": {c: 1.3 for c in AIRPORTS}},
                {"name": f"{net['refineries'][0]['id']} offline", "refineries_offline": [net["refineries"][0]["id"]]},
            ]
            for sc in examples:
                r = operate(bio, ap, A, net, sc)
                r["checks"] = check(r)
                fn = sc["name"].lower().replace(" ", "_").replace(":", "").replace("%", "pct").replace("+", "plus")
                save(r, f"{out}/scenario_{fn}.json")
                print(f"Scenario '{sc['name']}': transport ${r['cost']['transport_per_year']/1e6:.1f}M/yr, "
                      f"short {sum(r['shortfall_t'].values())/1e3:.0f} kt, checks "
                      f"{sum(c['pass'] for c in r['checks'])}/{len(r['checks'])} pass")
        if cmd in ("all", "refine", "stress"):
            rows = stress(bio, ap, A, net, base)
            save(rows, f"{out}/stress_test.json")
            print("\nStress test:")
            print(pd.DataFrame(rows).to_string(index=False))
