"""Fair baseline: a sensible planner's sketch WITHOUT optimisation.
- Depots: k-means clusters of farms (weighted by tonnes), all LARGE, k = tonnes needed / 300 kt
- Refineries: k-means clusters of farms, all LARGE, k = tonnes needed / 1,087 kt
- Then route material with the same LP (so routing is optimal; only placement and sizing are naive)
Usage: SAF_DEMAND_T=1088000 python baseline_kmeans.py biomass.xlsx Airport.dat.xlsx results_80pct
"""
import json
import math
import sys

from sklearn.cluster import KMeans

import saf_model as sm

bio, ap = sm.load_data(sys.argv[1], sys.argv[2])
out = sys.argv[3]
A = sm.ASSUMPTIONS
demand = sm.airport_demand(bio, ap, A)
feed_needed = sum(demand.values()) / A["saf_yield"]           # t refinery feed per year
bio_needed = feed_needed / A["depot_mass_yield"]               # t biomass per year
X = bio[["latitude", "longitude"]].to_numpy()
w = bio.production_tonnes.to_numpy(float)

k_dep = math.ceil(bio_needed / sm.DEPOT_SIZES["L"]["cap_t"])
k_ref = math.ceil(feed_needed / sm.REFINERY_SIZES["L"]["cap_t"])
cd = KMeans(n_clusters=k_dep, n_init=10, random_state=1).fit(X, sample_weight=w).cluster_centers_
cr = KMeans(n_clusters=k_ref, n_init=10, random_state=1).fit(X, sample_weight=w).cluster_centers_

net = {
    "depots": [{"id": f"D{i+1:02d}", "lat": float(a), "lon": float(b), "size": "L",
                "cap_t": sm.DEPOT_SIZES["L"]["cap_t"], "capex": sm.DEPOT_SIZES["L"]["capex"]}
               for i, (a, b) in enumerate(cd)],
    "refineries": [{"id": f"R{i+1}", "lat": float(a), "lon": float(b), "size": "L",
                    "cap_t": sm.REFINERY_SIZES["L"]["cap_t"], "capex": sm.REFINERY_SIZES["L"]["capex"]}
                   for i, (a, b) in enumerate(cr)],
}
for fac in net["depots"] + net["refineries"]:          # same on-land rule as the optimised design
    fac["lat"], fac["lon"] = sm.snap_to_land(fac["lat"], fac["lon"])
r = sm.operate(bio, ap, A, net, {"name": "k-means baseline"}, design_mode=True)
r["checks"] = sm.check(r)
opt = json.load(open(f"{out}/base.json"))
oc, bc = opt["cost"], r["cost"]
r["comparison"] = {
    "baseline_total_annual": bc["total_annual"], "optimised_total_annual": oc["total_annual"],
    "baseline_excl_imports_annual": bc["total_annual"] - bc["imports_per_year"],
    "saving_pct_excl_imports": 100 * (1 - oc["total_annual"] / (bc["total_annual"] - bc["imports_per_year"])),
    "baseline_short_kt": sum(r["shortfall_t"].values()) / 1e3,
    "baseline_facilities": f"{k_dep} large depots + {k_ref} large refineries",
    "baseline_cost": bc, "optimised_cost": oc,
}
sm.save(r, f"{out}/baseline_kmeans.json")
c = r["comparison"]
print(f"Baseline: {c['baseline_facilities']}: ${c['baseline_excl_imports_annual']/1e6:.1f}M/yr excl. imports, "
      f"{c['baseline_short_kt']:.0f} kt/yr SAF undelivered")
print(f"Optimised: ${c['optimised_total_annual']/1e6:.1f}M/yr, all demand met")
print(f"Saving (like-for-like, excl. import penalty): {c['saving_pct_excl_imports']:.1f}%")
print("Checks:", sum(x["pass"] for x in r["checks"]), "/", len(r["checks"]))
