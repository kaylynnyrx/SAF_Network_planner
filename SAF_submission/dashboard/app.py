"""
SAF Network Planner: Kalimantan (Airbus Challenge #3)
Run:  streamlit run app.py
"""
import json
import math
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import saf_model as sm

HERE = os.path.dirname(os.path.abspath(__file__))
st.set_page_config(page_title="SAF Network Planner", layout="wide")


# ================================================================ data
@st.cache_resource
def load():
    bio, ap = sm.load_data(os.path.join(HERE, "biomass.xlsx"), os.path.join(HERE, "Airport.dat.xlsx"))
    base = json.load(open(os.path.join(HERE, "results", "base.json")))
    A = dict(sm.ASSUMPTIONS)
    A.update({k: base["assumptions"][k] for k in base["assumptions"]})
    extra = {}
    for name in ["baseline_kmeans.json", "sensitivity.json", "stress_test.json", "truck_trips.json"]:
        p = os.path.join(HERE, "results", name)
        if os.path.exists(p):
            extra[name] = json.load(open(p))
    return bio, ap, base, A, extra


bio, ap, base, A, extra = load()
net = base["network"]
DISTRICTS = sorted(bio.district.unique())
REFS = [r["id"] for r in net["refineries"]]
APS = {a["iata"]: a["short"] for a in base["airports"]}
BASE_OP = base["cost"]["transport_per_year"] + base["cost"]["variable_opex_per_year"]
BASE_CO2 = base["emissions"]["total_t_per_year"]
st.session_state.setdefault("runs", [])
st.session_state.setdefault("decisions", [])


def solve_plans(sc):
    """All plan options for a scenario; each result gets a run number and sanity checks."""
    plans = sm.plan_options(bio, ap, A, net, sc)
    for p in plans:
        p["result"]["checks"] = sm.check(p["result"])
        p["result"]["run_id"] = len(st.session_state.runs) + 1
        st.session_state.runs.append(p["result"])
    return plans


def summary(r, weeks=None):
    """Compact numbers for display and for Claude to explain (no flows)."""
    op = r["cost"]["transport_per_year"] + r["cost"]["variable_opex_per_year"]
    s = {
        "run_id": r["run_id"], "scenario": r["scenario"], "plan": sm.PLAN_MODES.get(r.get("objective", "cost")),
        "operating_cost_musd_per_yr": round(op / 1e6, 1), "base_operating_cost_musd_per_yr": round(BASE_OP / 1e6, 1),
        "logistics_co2e_kt_per_yr": round(r["emissions"]["total_t_per_year"] / 1e3, 1),
        "base_logistics_co2e_kt_per_yr": round(BASE_CO2 / 1e3, 1),
        "saf_shortfall_kt_per_yr": {k: round(v / 1e3, 1) for k, v in r["shortfall_t"].items()},
        "saf_demand_kt_per_yr": {k: round(v / 1e3, 1) for k, v in r["demand_t"].items()},
        "refinery_feed_kt": {x["id"]: round(sum(f["t"] for f in r["flows"]["depot_refinery"] if f["to"] == x["id"]) / 1e3, 1)
                             for x in net["refineries"]},
        "refinery_capacity_kt": {x["id"]: x["cap_t"] / 1e3 for x in net["refineries"]},
        "checks_passed": f'{sum(c["pass"] for c in r["checks"])}/{len(r["checks"])}',
    }
    if weeks:
        f = weeks / 52
        s["event_weeks"] = weeks
        s["over_event_saf_short_kt"] = round(sum(r["shortfall_t"].values()) * f / 1e3, 1)
        s["over_event_extra_operating_cost_musd"] = round((op - BASE_OP) * f / 1e6, 2)
        s["over_event_extra_co2e_kt"] = round((r["emissions"]["total_t_per_year"] - BASE_CO2) * f / 1e3, 2)
    return s


# ================================================================ sidebar
with st.sidebar:
    st.header("What to test")
    kind = st.radio("Mode", ["Stress test (whole year)", "Event forecast"],
                    help="Event forecast: a specific event with a place, duration and severity. "
                         "Stress test: a condition lasting a full year.")
    sc = {"name": "Base case"}
    weeks = None
    if kind == "Event forecast":
        n_ev = st.number_input("How many events at the same time?", 1, 3, 1)
        evs, wks, parts = [], [], []
        for n in range(int(n_ev)):
            with st.expander(f"Event {n+1}", expanded=(n == n_ev - 1)):
                etype = st.selectbox("Type", ["Flood", "Drought / fire & haze", "Refinery outage", "Demand surge"], key=f"t{n}")
                if etype in ("Flood", "Drought / fire & haze"):
                    where = st.selectbox("Centred on district", DISTRICTS, index=(DISTRICTS.index("Ketapang") + 3 * n) % len(DISTRICTS), key=f"d{n}")
                    d = bio[bio.district == where]
                    lat = st.number_input("Latitude", value=float(round(d.latitude.mean(), 3)), format="%.3f", key=f"la{n}")
                    lon = st.number_input("Longitude", value=float(round(d.longitude.mean(), 3)), format="%.3f", key=f"lo{n}")
                    radius = st.slider("Radius (km)", 10, 250, 60, 10, key=f"r{n}")
                    sev = st.slider("% of biomass output lost", 10, 100, 70 if etype == "Flood" else 40, 5, key=f"s{n}")
                    hits = st.checkbox("Also damages depots/refineries in the area", value=(etype == "Flood"), key=f"h{n}")
                    w = st.slider("Duration (weeks)", 1, 52, 3 if etype == "Flood" else 12, key=f"w{n}")
                    evs.append({"lat": lat, "lon": lon, "radius_km": radius, "severity": sev / 100, "weeks": w,
                                "hits_facilities": hits, "type": etype})
                    parts.append(f"{etype.split(' ')[0]} {where} -{sev}% {w}wk")
                elif etype == "Refinery outage":
                    rid = st.selectbox("Refinery", REFS, key=f"rf{n}")
                    loss = st.slider("Capacity lost (%)", 10, 100, 100, 10, key=f"rl{n}")
                    w = st.slider("Duration (weeks)", 1, 52, 4, key=f"w{n}")
                    sc.setdefault("refinery_capacity_factor", {})[rid] = 1 - loss / 100
                    parts.append(f"{rid} -{loss}% {w}wk")
                else:
                    which = st.multiselect("Airports", list(APS), default=list(APS), format_func=lambda c: f"{APS[c]} ({c})", key=f"a{n}")
                    up = st.slider("Demand increase (%)", 5, 100, 30, 5, key=f"u{n}")
                    w = st.slider("Duration (weeks)", 1, 52, 3, key=f"w{n}")
                    for c in which:
                        sc.setdefault("demand_factor", {})[c] = sc.get("demand_factor", {}).get(c, 1.0) * (1 + up / 100)
                    parts.append(f"demand +{up}% {w}wk")
                wks.append(w)
        if evs:
            sc["events"] = evs
        weeks = max(wks)
        sc["name"] = " + ".join(parts)
        st.caption("Events are treated as overlapping for the longest duration (a conservative simplification).")
    else:
        floods = st.multiselect("Districts flooded all year (output = 0)", DISTRICTS)
        drought_d = st.multiselect("Districts in drought", DISTRICTS)
        drought_f = st.slider("Drought: % of normal output", 0, 100, 50, 5) / 100
        refs_off = st.multiselect("Refineries offline all year", REFS)
        dem = {c: 1 + st.slider(f"{APS[c]} ({c}) demand change %", -50, 100, 0, 5) / 100 for c in APS}
        if floods: sc["districts_offline"] = floods
        if drought_d: sc["district_supply_factor"] = {d: drought_f for d in drought_d}
        if refs_off: sc["refineries_offline"] = refs_off
        if any(abs(v - 1) > 1e-9 for v in dem.values()): sc["demand_factor"] = dem
        if len(sc) > 1: sc["name"] = "Custom stress test"
    with st.expander("Other disruptions (combine with the above)"):
        fuel = st.slider("Fuel / haulage price change (%)", -30, 100, 0, 5)
        tarakan = st.checkbox("Tarakan barge / port closed (Juwata cut off)")
        roads = st.multiselect("Roads closed (depot → refinery links)",
                               [f"{f['from']}→{f['to']}" for f in base["flows"]["depot_refinery"]])
        if fuel: sc["transport_cost_factor"] = 1 + fuel / 100
        if tarakan: sc["airports_unreachable"] = ["TRK"]
        if roads: sc["blocked_links"] = [x.split("→") for x in roads]
        extra_bits = ([f"fuel {fuel:+d}%"] if fuel else []) + (["Tarakan closed"] if tarakan else []) + ([f"{len(roads)} road(s) closed"] if roads else [])
        if extra_bits:
            sc["name"] = (sc["name"] + " + " if sc["name"] != "Base case" else "") + " + ".join(extra_bits)
    run_btn = st.button("Run", type="primary", use_container_width=True)
    st.caption(f"Design fixed: {len(net['depots'])} depots, {len(net['refineries'])} refineries. "
               "Scenarios re-route material; nothing new is built.")

key = json.dumps(sc, sort_keys=True)
if run_btn or "plans" not in st.session_state or st.session_state.get("plans_key") is None:
    st.session_state.plans = solve_plans(sc)
    st.session_state.plans_key = key
    st.session_state.weeks = weeks
plans = st.session_state.plans
weeks = st.session_state.weeks
if st.session_state.plans_key != key:
    st.sidebar.warning("Settings changed: click Run to update.")

# ================================================================ header + plan choice
st.title("SAF network planner: Kalimantan")
scen = plans[0]["result"]["scenario"]
st.caption(f"Scenario: {scen.get('name')}" + (f" · event lasts {weeks} weeks" if weeks else " · annual rates"))

st.subheader("Plan options: you choose")
st.caption("Same facilities, different priorities. Each plan is solved by the optimiser; pros and cons are computed by rules, not by AI.")
tbl = pd.DataFrame([{
    "Plan": p["plan"],
    "Operating cost $M/yr": round(p["operating_cost_musd"], 1),
    "Logistics CO2e kt/yr": round(p["co2_kt"], 1),
    "SAF demand met %": round(p["demand_met_pct"], 1),
    "Worst airport met %": round(p["worst_airport_met_pct"]),
    "Shortfall kt/yr": ", ".join(f"{k} {v:.0f}" for k, v in p["short_kt"].items()) or "none",
    "Longest SAF haul km": round(p["longest_saf_haul_km"]),
} for p in plans])
st.dataframe(tbl, hide_index=True, use_container_width=True)
if max(p["operating_cost_musd"] for p in plans) - min(p["operating_cost_musd"] for p in plans) < 0.5 and \
        max(p["co2_kt"] for p in plans) - min(p["co2_kt"] for p in plans) < 0.5 and \
        len({json.dumps({k: round(v) for k, v in p["short_kt"].items()}, sort_keys=True) for p in plans}) == 1:
    st.info("All four plans give practically the same result here: there is no real trade-off to make in this scenario.")
cols = st.columns(len(plans))
for col, p in zip(cols, plans):
    with col.container(border=True):
        st.markdown(f"**{p['plan']}**")
        st.markdown("\n".join(f"- ✅ {x}" for x in p["pros"]) or "- ✅ none stands out")
        st.markdown("\n".join(f"- ⚠️ {x}" for x in p["cons"]) or "- ⚠️ no major drawback vs the others")
choice = st.radio("Show plan", [p["plan"] for p in plans], horizontal=True)
P = next(p for p in plans if p["plan"] == choice)
r = P["result"]
c1, c2 = st.columns([1, 5])
if c1.button("Approve this plan"):
    st.session_state.decisions.append({"run": r["run_id"], "scenario": scen.get("name"), "plan": choice, "decision": "approved"})
    c2.success(f"Approved: {choice} for '{scen.get('name')}' (run #{r['run_id']}). Logged in Run history.")

# ================================================================ KPIs
short_kt = sum(r["shortfall_t"].values()) / 1e3
dem_kt = sum(r["demand_t"].values()) / 1e3
op = r["cost"]["transport_per_year"] + r["cost"]["variable_opex_per_year"]
em = r["emissions"]
k1, k2, k3, k4 = st.columns(4)
k1.metric("Operating cost", f"${op/1e6:,.0f}M/yr", (f"{(op-BASE_OP)/1e6:+.1f}M vs base" if abs(op - BASE_OP) > 5e4 else None),
          delta_color="inverse", help="Variable OpEx + transport. Falls when less is moved, so read it with SAF delivered. "
                                      "Design's full annual cost incl. CapEx and fixed OpEx: "
                                      f"${base['cost']['total_annual']/1e6:,.0f}M/yr.")
k2.metric("SAF delivered", f"{dem_kt-short_kt:,.0f} kt/yr", f"{-short_kt:,.0f} kt short" if short_kt > 0.5 else "all demand met",
          delta_color="normal")
k3.metric("Logistics CO2e", f"{em['total_t_per_year']/1e3:,.0f} kt/yr",
          (f"{(em['total_t_per_year']-BASE_CO2)/1e3:+.1f} kt vs base" if abs(em['total_t_per_year'] - BASE_CO2) > 50 else None),
          delta_color="inverse", help=f"{em['kg_per_t_saf']:.0f} kg CO2e per t SAF = {em['g_per_MJ']:.2f} g/MJ, "
                                      f"{em['pct_of_fossil_jet_baseline']:.1f}% of fossil jet's 89 g/MJ life-cycle baseline (ICAO). "
                                      "Logistics only: refinery process and farming emissions not included.")
k4.metric("Carbon intensity", f"{em['g_per_MJ']:.2f} g/MJ", f"{em['pct_of_fossil_jet_baseline']:.1f}% of fossil jet baseline",
          delta_color="off")
if weeks:
    f = weeks / 52
    repl = short_kt * 1e3 * A["import_cost_per_t_saf"]
    st.info(f"**Over the {weeks}-week event:** SAF short **{short_kt*f:,.1f} kt**. Operating cost "
            f"**\\${(op-BASE_OP)*f/1e6:+,.2f}M** (lower when less is moved), plus about **\\${repl*f/1e6:,.1f}M** to replace the "
            f"missing SAF at \\${A['import_cost_per_t_saf']:,}/t (our assumption): net **\\${(op-BASE_OP+repl)*f/1e6:+,.1f}M**. "
            f"Logistics CO2e **{(em['total_t_per_year']-BASE_CO2)*f/1e3:+,.2f} kt**. "
            "(Annual rates × weeks/52; no storage assumed.)")
if r.get("event"):
    st.caption(f"Event area hits {len(r['event']['farms_hit'])} biomass sites"
               + (f" and facilities {', '.join(r['event']['facilities_hit'])}" if r["event"]["facilities_hit"] else ""))

# ================================================================ map
pos = {f["id"]: (f["lat"], f["lon"]) for f in net["depots"] + net["refineries"]}
pos.update({a["iata"]: (a["lat"], a["lon"]) for a in base["airports"]})
pos.update({row.location_id: (row.latitude, row.longitude) for row in bio.itertuples()})
fig = go.Figure()
for ring in json.load(open(os.path.join(HERE, "results", "borneo_coast.json"))):
    fig.add_trace(go.Scatter(x=[c[0] for c in ring], y=[c[1] for c in ring], mode="lines", fill="toself",
                             fillcolor="#F1EFE8", line=dict(color="#B4B2A9", width=1), hoverinfo="skip", showlegend=False))
for n_, ev in enumerate(r.get("events") or []):
    ang = [i * 2 * math.pi / 72 for i in range(73)]
    dlat = ev["radius_km"] / 111.0
    dlon = dlat / max(math.cos(math.radians(ev["lat"])), 0.1)
    fig.add_trace(go.Scatter(x=[ev["lon"] + dlon * math.cos(t) for t in ang], y=[ev["lat"] + dlat * math.sin(t) for t in ang],
                             mode="lines", fill="toself", fillcolor="rgba(226,75,74,0.12)", line=dict(color="#E24B4A", dash="dot"),
                             name=f"Event {n_+1}: {ev.get('type', 'event')} ({ev['radius_km']} km, -{ev['severity']:.0%})",
                             hoverinfo="skip"))
rate = {"farm_depot": 0.10, "depot_refinery": 0.12, "refinery_airport": 0.08}


def add_lines(leg, color, scale, name, dash=None):
    first = True
    for f in r["flows"][leg]:
        (la1, lo1), (la2, lo2) = pos[f["from"]], pos[f["to"]]
        fig.add_trace(go.Scatter(x=[lo1, lo2], y=[la1, la2], mode="lines",
                                 line=dict(width=max(0.6, f["t"] / scale), color=color, dash=dash),
                                 name=name, legendgroup=name, showlegend=first, hoverinfo="text",
                                 text=(f'{f["from"]} → {f["to"]}: {f["t"]/1e3:,.0f} kt/yr, {f["km"]:.0f} km, '
                                       f'${f["t"]*f["km"]*rate[leg]/1e6:,.2f}M/yr, {f["co2_t"]/1e3:,.2f} kt CO2e/yr')))
        first = False


add_lines("farm_depot", "rgba(29,158,117,0.55)", 60000, "Biomass → depot")
add_lines("depot_refinery", "rgba(127,119,221,0.8)", 60000, "Depot → refinery")
add_lines("refinery_airport", "rgba(216,90,48,0.9)", 25000, "SAF → airport", dash="dash")
farm = bio.assign(now=bio.location_id.map(r["supply_t"]))
normal, hit = farm[farm.now >= farm.production_tonnes - 1], farm[farm.now < farm.production_tonnes - 1]
fig.add_trace(go.Scatter(x=normal.longitude, y=normal.latitude, mode="markers", name="Biomass site",
                         marker=dict(size=normal.production_kilotons / 14 + 3, color="#1D9E75"),
                         text=[f"{x.location_id} · {x.district}<br>{x.production_kilotons:.0f} kt/yr" for x in normal.itertuples()],
                         hoverinfo="text"))
if len(hit):
    fig.add_trace(go.Scatter(x=hit.longitude, y=hit.latitude, mode="markers", name="Site hit",
                             marker=dict(size=hit.production_kilotons / 14 + 3, color="#EF9F27", symbol="x"),
                             text=[f"{x.location_id} · {x.district}<br>now {x.now/1e3:.0f} of {x.production_kilotons:.0f} kt"
                                   for x in hit.itertuples()], hoverinfo="text"))
hit_fac = set((r.get("event") or {}).get("facilities_hit", [])) | set(scen.get("refineries_offline", [])) \
    | {k for k, v in scen.get("refinery_capacity_factor", {}).items() if v < 1}
dep = pd.DataFrame(net["depots"])
fig.add_trace(go.Scatter(x=dep.lon, y=dep.lat, mode="markers", name="Depot",
                         marker=dict(size=dep["size"].map({"S": 7, "L": 10}), symbol="square",
                                     color=["#B4B2A9" if i in hit_fac else "#534AB7" for i in dep.id]),
                         text=[f'{d.id} depot ({d.size}) · {d.cap_t/1e3:.0f} kt cap' for d in dep.itertuples()], hoverinfo="text"))
ref = pd.DataFrame(net["refineries"])
fig.add_trace(go.Scatter(x=ref.lon, y=ref.lat, mode="markers+text", name="Refinery",
                         marker=dict(size=ref["size"].map({"S": 14, "L": 19}), symbol="triangle-up",
                                     color=["#B4B2A9" if i in hit_fac else "#993C1D" for i in ref.id], line=dict(width=1, color="white")),
                         textposition="top center", text=[f"{x.id}{' (hit)' if x.id in hit_fac else ''}" for x in ref.itertuples()],
                         hovertext=[f'{x.id} refinery ({x.size}) · {x.cap_t/1e3:,.0f} kt cap' for x in ref.itertuples()], hoverinfo="text"))
apdf = pd.DataFrame(base["airports"])
apdf["sh"] = apdf.iata.map(lambda c: r["shortfall_t"].get(c, 0) / 1e3)
fig.add_trace(go.Scatter(x=apdf.lon, y=apdf.lat, mode="markers+text", name="Airport",
                         marker=dict(size=16, symbol="star", color=["#E24B4A" if s > 0.5 else "#185FA5" for s in apdf.sh]),
                         textposition="bottom center", text=[f"{a.iata}{f' −{a.sh:.0f} kt' if a.sh > 0.5 else ''}" for a in apdf.itertuples()],
                         hovertext=[f"{a.iata}: demand {r['demand_t'][a.iata]/1e3:.0f} kt, short {a.sh:.0f} kt" for a in apdf.itertuples()],
                         hoverinfo="text"))
fig.update_layout(height=620, margin=dict(l=0, r=0, t=0, b=0), legend=dict(orientation="h", y=-0.04),
                  plot_bgcolor="#E6F1FB", hovermode="closest",
                  xaxis=dict(range=[108.6, 119.2], showgrid=False, zeroline=False, ticksuffix="°E"),
                  yaxis=dict(range=[-4.6, 4.4], showgrid=False, zeroline=False, ticksuffix="°", scaleanchor="x", scaleratio=1))

left, right = st.columns([2.2, 1])
with left:
    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"Plan shown: {choice}. Hover a line for tonnes, km, cost and CO2e. Distances = great-circle × {A['road_factor']}.")
with right:
    st.subheader("Airport supply")
    st.dataframe(pd.DataFrame([{"Airport": f"{APS[c]} ({c})", "Demand kt": round(r["demand_t"][c] / 1e3),
                                "Delivered kt": round((r["demand_t"][c] - r["shortfall_t"][c]) / 1e3),
                                "Short kt": round(r["shortfall_t"][c] / 1e3)} for c in APS]),
                 hide_index=True, use_container_width=True)
    st.subheader("Refinery load")
    rows = []
    for x in net["refineries"]:
        feed = sum(f["t"] for f in r["flows"]["depot_refinery"] if f["to"] == x["id"])
        rows.append({"Refinery": x["id"], "Size": x["size"], "Feed kt": round(feed / 1e3), "Use %": round(100 * feed / x["cap_t"])})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                 column_config={"Use %": st.column_config.ProgressColumn("Use %", min_value=0, max_value=100, format="%d%%")})
    with st.expander("Assumptions (always state these)", expanded=False):
        ef = A["emission_factors"]
        st.markdown(f"""
- SAF yield **{A['saf_yield']:.0%}**, demand **300 kt/yr per airport**, capacity = biomass intake *(organiser)*
- Cost/yr = CapEx/{A['years']} + 5% CapEx + variable OpEx + transport; distance = great-circle × **{A['road_factor']}** *(organiser)*
- Facilities on land (Natural Earth 1:10m) *(organiser rule)*
- Emission factors, kg CO2e/t-km: small trucks **{ef['farm_depot']}**, large trucks **{ef['depot_refinery']}**, barge **{ef['barge']}** (GLEC defaults; indicative)
- Fossil jet baseline **89 g CO2e/MJ** (ICAO CORSIA); jet fuel **43 MJ/kg** *(our assumption)*
- Juwata: last **{A['barge_km_to_TRK']:.0f} km** by barge *(our assumption)*
- Balanced plan carbon price **\\${A['carbon_price_usd_per_t']:.0f}/t CO2e** *(our assumption)*
- Disruptions only: undelivered SAF priced at **\\${A['import_cost_per_t_saf']:,}/t**; re-routing up to **{A['max_farm_depot_km_disrupted']} km** *(our assumptions)*
- Events: annual rates × weeks/52, no storage *(our simplification)*
""")

# ================================================================ Claude what-if
st.divider()
st.subheader("Ask a what-if")
st.caption("Claude turns your question into solver settings, the solver runs every plan option, then Claude explains using only the solver's numbers.")

SCHEMA_PROMPT = f"""You convert a supply planner's question into a JSON scenario for an SAF supply-chain solver in Kalimantan.
Output ONLY one JSON object, no prose. Allowed keys (omit unused ones):
- "name": short title
- "events": a LIST of localised events, each {{"lat": number, "lon": number, "radius_km": number,
            "severity": fraction of biomass output lost 0-1, "weeks": duration, "hits_facilities": true/false,
            "type": "Flood" or "Drought" or "Fire"}}. Several events can happen at once.
  District centres (lat, lon): {json.dumps({d: [round(bio[bio.district == d].latitude.mean(), 3), round(bio[bio.district == d].longitude.mean(), 3)] for d in DISTRICTS})}
- "districts_offline": list from {DISTRICTS} (whole district produces nothing all year)
- "district_supply_factor": {{district: fraction 0-1}}
- "refineries_offline": list from {REFS}
- "refinery_capacity_factor": {{refinery: fraction 0-1}}
- "demand_factor": {{airport_code: multiplier}}, codes: {json.dumps(APS)}
- "transport_cost_factor": multiplier on haulage cost, e.g. 1.3 for a 30% fuel price rise
- "airports_unreachable": list of airport codes cut off (e.g. ["TRK"] if the Tarakan barge/port is closed)
- "blocked_links": list of [from_id, to_id] facility pairs whose road is closed, ids like "D03" and "R2"
- "weeks": duration in weeks if the question gives one and there is no "events" list
Combine as many keys as the question needs: simultaneous disruptions are expected.
If the question cannot be expressed with these keys (e.g. soil quality, prices, weather forecasts, anything not in the model),
output {{"unsupported": "<one-sentence reason>"}}. Never invent districts, refineries or airports."""

EXPLAIN_PROMPT = """You explain SAF supply-chain solver results to a supply planner in 4-6 short sentences.
Use ONLY numbers present in the JSON; never compute new figures or estimate. Cite runs as [run #N].
The JSON holds several plan options for the same scenario (cheapest, lowest carbon, balanced, fair shares).
Say what changes versus the base case, then contrast the plans on cost, CO2e and which airports go short.
Name the binding constraint (e.g. refinery capacity near 100%, lost biomass). Do not pick a plan: the planner decides."""


def reply_text(msg):
    return "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text").strip()


def validate(q):
    ok = {"name", "events", "districts_offline", "district_supply_factor", "refineries_offline",
          "refinery_capacity_factor", "demand_factor", "weeks", "transport_cost_factor", "airports_unreachable", "blocked_links"}
    bad = [k for k in q if k not in ok]
    bad += [d for d in q.get("districts_offline", []) + list(q.get("district_supply_factor", {})) if d not in DISTRICTS]
    bad += [x for x in q.get("refineries_offline", []) + list(q.get("refinery_capacity_factor", {})) if x not in REFS]
    bad += [c for c in q.get("demand_factor", {}) if c not in APS]
    for ev in q.get("events", []):
        if not all(k in ev for k in ("lat", "lon", "radius_km", "severity")):
            bad.append("each event needs lat, lon, radius_km, severity")
    bad += [c for c in q.get("airports_unreachable", []) if c not in APS]
    ids = {f["id"] for f in net["depots"] + net["refineries"]}
    bad += [l for l in q.get("blocked_links", []) if len(l) != 2 or l[0] not in ids or l[1] not in ids]
    return bad


qtext = st.text_input("Question", placeholder="A 3-week flood hits Kotawaringin Timur, about 80 km wide, half the crop lost. What happens?")
if st.button("Ask Claude") and qtext:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        st.warning("Set ANTHROPIC_API_KEY first (see README), then restart the app.")
    else:
        import anthropic
        client = anthropic.Anthropic()
        MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
        with st.spinner("Claude is translating your question..."):
            raw = reply_text(client.messages.create(model=MODEL, max_tokens=600, system=SCHEMA_PROMPT,
                                                    messages=[{"role": "user", "content": qtext}]))
        try:
            q = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError):
            q = {"unsupported": "Could not parse a scenario from that question."}
        if "unsupported" in q:
            st.info(f"Not something this model can answer: {q['unsupported']}")
        elif validate(q):
            st.error(f"Claude proposed settings the model doesn't have: {validate(q)}. Nothing was run.")
        else:
            st.markdown("**Solver settings Claude proposed** (check before trusting):")
            st.json(q)
            qweeks = max([e.get("weeks") or 0 for e in q.get("events", [])] + [q.pop("weeks", None) or 0]) or None
            qplans = solve_plans(q)
            with st.spinner("Claude is comparing the plan options..."):
                txt = reply_text(client.messages.create(
                    model=MODEL, max_tokens=700, system=EXPLAIN_PROMPT,
                    messages=[{"role": "user", "content": json.dumps([summary(p["result"], qweeks) for p in qplans])}]))
            st.markdown(txt)
            st.dataframe(pd.DataFrame([{"Plan": p["plan"], "Run": p["result"]["run_id"],
                                        "Operating $M/yr": round(p["operating_cost_musd"], 1), "CO2e kt/yr": round(p["co2_kt"], 1),
                                        "Demand met %": round(p["demand_met_pct"], 1),
                                        "Short kt/yr": ", ".join(f"{k} {v:.0f}" for k, v in p["short_kt"].items()) or "none"}
                                       for p in qplans]), hide_index=True, use_container_width=True)
            st.caption("To approve one of these plans, set the same scenario in the sidebar and use 'Approve this plan'.")

# ================================================================ detail tabs
st.divider()
tg, t0, t1, t2, t3, t4, t5 = st.tabs(["How to use", "Routes & carbon", "Where we build & why", "Stress test", "Robustness",
                                       "Sanity checks", "Run history"])
with tg:
    g1, g2 = st.columns(2)
    with g1:
        st.markdown("""
#### ✅ Use it to
- **Compare responses to a disruption** before it happens: flood, drought or fire, refinery outage, demand surge,
  fuel price change, port or road closure, or several at once.
- **See the trade-off** between cost, carbon and which airports go short, then **choose and approve** a plan.
- **Size the impact** of an event: SAF short, extra cost, extra CO2e over its duration.
- **Find weak points:** which refinery or district, if lost, hurts most: open the **Stress test tab** (not the
  Ask box, which runs one scenario per question).
- **Ask in plain English** (Ask a what-if): Claude turns the question into solver settings that are shown to you,
  the solver runs, and Claude explains using only the solver's numbers.
- **Check every number:** run IDs, 10 sanity checks per run, assumptions always listed.
""")
    with g2:
        st.markdown("""
#### ⚠️ Don't use it to
- **Decide where to build** without re-running the design: scenarios re-route through the **fixed** 23 depots and
  6 refineries; they never build or move facilities (re-run the notebook for that).
- **Plan day-to-day truck dispatch:** results are yearly rates; a 3-week event is scaled as rate × weeks/52,
  with no storage, recovery time or inventory.
- **Navigate real roads:** distances are great-circle × 1.3 (organiser basis), not a road network.
- **Get prices, weather forecasts, soil quality, labour or regulation answers:** not in the model; Claude should decline.
- **Treat the CO2e as a full life-cycle figure:** it is logistics only, with indicative GLEC factors.
- **Trust the "replace missing SAF" cost as a quote:** $3,000/t is our assumption.
- **Let Claude decide:** it never picks a plan; always check the settings it proposes before trusting the result.
""".replace("$3,000", "\\$3,000"))
    st.markdown("""
#### What the "Ask a what-if" box can and can't do
| Can | Can't |
|---|---|
| Combine several disruptions in one question | Invent districts, refineries or airports (rejected, nothing is run) |
| Floods, droughts or fires with place, radius, severity, weeks | Questions outside the model (it says so instead of guessing) |
| Refinery or depot outages (full or partial) | Do arithmetic or pick a plan: the solver computes, you decide |
| Demand changes, fuel price, port and road closures | Remember earlier questions (each question is independent) |
| One scenario per question | Rank many scenarios ("which refinery hurts most?"): it declines and points you to one-at-a-time; use the Stress test tab |

Needs an Anthropic API key set in Terminal. Results are only as good as the settings it proposes, so read them first.
""")
with t0:
    rows = []
    for leg, fl in r["flows"].items():
        for f in fl:
            rows.append({"Leg": leg.replace("_", " → "), "From": f["from"], "To": f["to"], "Tonnes/yr": round(f["t"]),
                         "km": round(f["km"]), "Cost $/yr": round(f["t"] * f["km"] * rate[leg]),
                         "CO2e t/yr": round(f["co2_t"], 1), "kg CO2e/t": round(1000 * f["co2_t"] / f["t"], 1),
                         "Mode": f.get("mode", "truck")})
    routes = pd.DataFrame(rows).sort_values("CO2e t/yr", ascending=False)
    st.caption(f"{len(routes)} routes in the plan shown, sorted by emissions. Logistics total "
               f"{em['total_t_per_year']/1e3:,.1f} kt CO2e/yr: farm→depot {em['by_leg_t']['farm_depot']/1e3:,.1f}, "
               f"depot→refinery {em['by_leg_t']['depot_refinery']/1e3:,.1f}, refinery→airport {em['by_leg_t']['refinery_airport']/1e3:,.1f} kt.")
    st.dataframe(routes, hide_index=True, use_container_width=True)
with t1:
    j = pd.DataFrame(base["justification"])
    st.dataframe(j[["id", "type", "size", "lat", "lon", "near", "inflow_kt", "utilisation_pct", "customers", "avg_inbound_km", "why"]],
                 hide_index=True, use_container_width=True)
    if "truck_trips.json" in extra:
        st.markdown("**Truck trips per year (base case)**: tonnes on each route ÷ payload, rounded up per route. "
                    "Cost per t-km is the same for both truck sizes; Juwata's last leg is by barge.")
        st.dataframe(pd.DataFrame(extra["truck_trips.json"]), hide_index=True, use_container_width=True)
with t2:
    if "stress_test.json" in extra:
        st.caption("Whole-year stress test: each refinery offline, then each district flooded, one at a time (cheapest plan).")
        st.dataframe(pd.DataFrame(extra["stress_test.json"]), hide_index=True, use_container_width=True)
with t3:
    if "sensitivity.json" in extra:
        st.markdown("**If roads are more winding than assumed** (design re-optimised each time)")
        st.dataframe(pd.DataFrame(extra["sensitivity.json"]), hide_index=True, use_container_width=True)
    if "baseline_kmeans.json" in extra:
        b = extra["baseline_kmeans.json"]["comparison"]
        st.markdown(f"**vs a sensible clustering plan** ({b['baseline_facilities']}): similar cost "
                    f"(\\${b['baseline_excl_imports_annual']/1e6:,.0f}M/yr vs our \\${b['optimised_total_annual']/1e6:,.0f}M/yr) but it "
                    f"**leaves {b['baseline_short_kt']:.0f} kt/yr of SAF undelivered**; ours meets 100% of demand.")
with t4:
    for c in r["checks"]:
        st.markdown(f"{'✅' if c['pass'] else '❌'} **{c['check']}**: {c['detail']}")
with t5:
    st.dataframe(pd.DataFrame([{"run": x["run_id"], "scenario": x["scenario"].get("name"),
                                "plan": sm.PLAN_MODES.get(x.get("objective", "cost")),
                                "operating $M/yr": round((x["cost"]["transport_per_year"] + x["cost"]["variable_opex_per_year"]) / 1e6, 1),
                                "CO2e kt/yr": round(x["emissions"]["total_t_per_year"] / 1e3, 1),
                                "short kt": round(sum(x["shortfall_t"].values()) / 1e3, 1),
                                "checks": f'{sum(c["pass"] for c in x["checks"])}/{len(x["checks"])}'}
                               for x in st.session_state.runs]), hide_index=True, use_container_width=True)
    if st.session_state.decisions:
        st.markdown("**Approved plans**")
        st.dataframe(pd.DataFrame(st.session_state.decisions), hide_index=True, use_container_width=True)
