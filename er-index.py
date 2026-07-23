#!/usr/bin/env python3
"""
Indice di accessibilità ai servizi — comuni dell'Emilia-Romagna
==============================================================

Rebuilds the whole index from primary sources. Nothing is hand-entered.

    pip install requests pandas numpy openpyxl shapely
    python build_indice_servizi.py

Outputs
    comuni_base.csv     330 comuni: ISTAT code, name, province, centroid, population, elevation
    indicators.csv      + the 12 accessibility indicators
    indicators_plus.csv + hazard exposure, demography, tourism pressure, income
    scored.csv          + 6 domain scores, composite score, rank
    payload.json      compact payload consumed by the HTML tool

Sources
    comuni + centroids   OpenStreetMap admin_level=8 relations (Overpass)
    population           ISTAT POSAS, resident population 1 Jan 2025
    official names       ISTAT "Codici delle unità amministrative territoriali"
    elevation            EU-DEM 25 m via OpenTopoData
    services             OpenStreetMap POIs (Overpass)
    tourist beds         OpenStreetMap tourism=* inside comune polygons (openpolis GeoJSON)
    coastline / beaches  OpenStreetMap natural=coastline and natural=beach
    flood / landslide    ISPRA IdroGEO API (national mosaic of the Piani di Assetto Idrogeologico)
    income               ISTAT "A misura di Comune" tav. 2.1, on MEF tax return data

Method
    Four core domains (salute, mobilita, istruzione, quotidiano) form the base index.
    Two optional domains (rischio, vitalita) default to weight 0 and leave the base index
    untouched until you turn them on. Tourism pressure, income and age structure are carried
    as context and deliberately not scored: more tourism is neither good nor bad in itself.

    Every accessibility indicator is an estimated car travel time from the comune centroid to the
    nearest instance of a service (one indicator is a count within 5 km). Values are
    normalised with ISTAT goalposts (regional median = 100) and aggregated with AMPI,
    the same non-compensatory index ISTAT uses for the Indice di Fragilità Comunale:
    a lopsided profile scores lower than a balanced one with the same mean.

Known substitutions worth making
    * REPLACE the emergency-room layer. OSM `emergency=yes` does not distinguish
      DEA I from DEA II. Drop an `er_override.csv` with lat,lon columns next to this
      script and it is used instead — e.g. the Ministero della Salute HSP extract or
      the Regione Emilia-Romagna list of pronto soccorso.
    * If a routing service is reachable from your machine, replace `nearest_min()`
      with an OSRM /table call. The crow-flies + terrain-speed estimate below is a
      fallback and understates travel time in the Apennines.
"""

import json, os, re, time
import numpy as np
import pandas as pd
import requests

# --------------------------------------------------------------------------- config
REGION_BBOX = "43.30,8.80,45.75,13.30"      # padded: nearest service may be outside the region
ER_PROV = {"033": "PC", "034": "PR", "035": "RE", "036": "MO", "037": "BO",
           "038": "FE", "039": "RA", "040": "FC", "099": "RN"}
DETOUR = 1.35                                # road distance / crow-flies distance
SPEED_BANDS = [(100, 58), (300, 48), (600, 38), (10_000, 30)]   # (max elevation m, km/h)

HDR = {"User-Agent": "indice-servizi-er/1.0"}
OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.osm.ch/api/interpreter"]

CORE = {
    "salute":     ["m_er", "m_pharm", "m_doc", "m_vet"],
    "mobilita":   ["m_rail", "m_mjunc"],
    "istruzione": ["m_kinder", "m_school", "m_upper"],
    "quotidiano": ["m_super", "m_post", "n_retail5"],
}
OPTIONAL = {                                  # default weight 0: outside the base index
    "rischio":  ["flood_p3", "land_p34"],     # residents inside mapped flood / landslide hazard
    "vitalita": ["pop_trend", "oldindex"],    # demographic trajectory
}
DOMAINS = {**CORE, **OPTIONAL}
IND = [i for v in DOMAINS.values() for i in v]
POSITIVE = {"n_retail5", "pop_trend"}                        # everything else: lower is better
DIR = {i: (+1 if i in POSITIVE else -1) for i in IND}
CONTEXT = ["beach_km", "sea_km", "tour_p1000", "tour_units", "income",
           "pct65", "age_mean", "flood_p2"]
COAST_REF = {"beach_km", "sea_km"}   # compared to the coastal median, not the regional one
UNITS = {**{i: "min" for i in IND if i.startswith("m_")}, "n_retail5": "n",
         "flood_p3": "pct", "flood_p2": "pct", "land_p34": "pct",
         "pop_trend": "pctd", "oldindex": "idx", "income": "eur",
         "pct65": "pct", "age_mean": "yr", "tour_p1000": "n", "tour_units": "n",
         "beach_km": "km", "sea_km": "km"}
DEFAULT_W = {**{d: 1 for d in CORE}, **{d: 0 for d in OPTIONAL}}

LABELS = {
    "m_er": "Ospedale con pronto soccorso", "m_pharm": "Farmacia",
    "m_doc": "Ambulatorio / poliambulatorio", "m_vet": "Veterinario",
    "m_rail": "Stazione ferroviaria", "m_mjunc": "Casello autostradale",
    "m_kinder": "Asilo / scuola d'infanzia", "m_school": "Scuola (qualsiasi grado)",
    "m_upper": "Scuola superiore", "m_super": "Supermercato",
    "m_post": "Ufficio postale", "n_retail5": "Esercizi entro 5 km",
    "flood_p3": "Residenti in area a pericolosità idraulica elevata",
    "land_p34": "Residenti in area a rischio frana elevato",
    "pop_trend": "Variazione popolazione 2019–2025", "oldindex": "Indice di vecchiaia",
    "tour_p1000": "Strutture ricettive per 1.000 abitanti",
    "tour_units": "Strutture ricettive nel comune",
    "income": "Reddito imponibile per contribuente", "pct65": "Popolazione 65+",
    "beach_km": "Distanza dalla spiaggia più vicina",
    "sea_km": "Distanza dalla linea di costa",
    "age_mean": "Età media",
    "flood_p2": "Residenti in area a pericolosità idraulica media",
}
COASTLINE_BBOX = "43.80,11.90,45.10,12.90"   # the Emilia-Romagna littoral
COAST_TOL = 0.001    # ~100 m, absorbs the offset between the OSM coastline and the boundary file


# ----------------------------------------------------------------------- 1. sources
def overpass(query, tries=8):
    for i in range(tries):
        ep = OVERPASS[i % len(OVERPASS)]
        try:
            r = requests.post(ep, data={"data": query}, headers=HDR, timeout=600)
            if r.status_code == 200:
                return r.json()["elements"]
            print(f"   {ep.split('//')[1][:22]} -> {r.status_code}")
        except Exception as e:
            print(f"   {ep.split('//')[1][:22]} -> {type(e).__name__}")
        time.sleep(20 + 12 * i)
    raise RuntimeError("Overpass unavailable")


def cached(path, produce):
    if os.path.exists(path):
        return json.load(open(path))
    data = produce()
    json.dump(data, open(path, "w"))
    return data


def fetch_comuni():
    def go():
        q = (f'[out:json][timeout:300];rel["boundary"="administrative"]'
             f'["admin_level"="8"]({REGION_BBOX.replace("43.30,8.80,45.75,13.30", "43.60,9.10,45.20,12.90")});'
             f'out tags center;')
        els = overpass(q)
        return [e for e in els
                if str(e.get("tags", {}).get("ref:ISTAT", "")).zfill(6)[:3] in ER_PROV]
    return cached("comuni_raw.json", go)


POI_SETS = {
    "health":    ['nwr["amenity"="hospital"]', 'nwr["healthcare"="hospital"]',
                  'nwr["amenity"="pharmacy"]', 'nwr["amenity"="doctors"]',
                  'nwr["amenity"="clinic"]', 'nwr["amenity"="veterinary"]'],
    "mobility":  ['nwr["railway"="station"]', 'nwr["railway"="halt"]',
                  'node["highway"="motorway_junction"]'],
    "education": ['nwr["amenity"="school"]', 'nwr["amenity"="kindergarten"]'],
    "daily":     ['nwr["shop"="supermarket"]', 'nwr["shop"="convenience"]',
                  'nwr["amenity"="post_office"]', 'nwr["amenity"="bank"]',
                  'nwr["amenity"="library"]'],
}


def fetch_pois():
    out = {}
    for name, selectors in POI_SETS.items():
        q = (f"[out:json][timeout:400];("
             + "".join(f"{s}({REGION_BBOX});" for s in selectors)
             + ");out tags center;")
        out[name] = cached(f"poi_{name}.json", lambda q=q: overpass(q))
        print(f"  {name:10s} {len(out[name]):6d} POIs")
        time.sleep(5)
    return out


def fetch_population():
    if not os.path.exists("posas/POSAS_2025_it_Comuni.csv"):
        import zipfile, io
        z = requests.get("https://demo.istat.it/data/posas/POSAS_2025_it_Comuni.zip",
                         headers=HDR, timeout=300)
        zipfile.ZipFile(io.BytesIO(z.content)).extractall("posas")
    df = pd.read_csv("posas/POSAS_2025_it_Comuni.csv", sep=";", skiprows=1,
                     dtype={"Codice comune": str, "Età": str})
    df = df[df["Età"].str.strip() == "999"]          # 999 = age total row
    s = df.groupby("Codice comune")["Totale"].sum().astype(int)
    s.index = s.index.str.zfill(6)
    return s


def fetch_istat_names():
    if not os.path.exists("istat_comuni.csv"):
        r = requests.get("https://www.istat.it/storage/codici-unita-amministrative/"
                         "Elenco-comuni-italiani.csv", headers=HDR, timeout=300)
        open("istat_comuni.csv", "wb").write(r.content)
    df = pd.read_csv("istat_comuni.csv", sep=";", encoding="latin-1", dtype=str)
    df["istat"] = df["Codice Comune formato alfanumerico"].str.zfill(6)
    return df.set_index("istat")["Denominazione in italiano"]


def fetch_elevation(df):
    out = []
    for i in range(0, len(df), 100):
        chunk = df.iloc[i:i + 100]
        locs = "|".join(f"{r.lat},{r.lon}" for r in chunk.itertuples())
        for _ in range(4):
            try:
                j = requests.get("https://api.opentopodata.org/v1/eudem25m",
                                 params={"locations": locs}, headers=HDR, timeout=120).json()
                if j.get("status") == "OK":
                    out += [x["elevation"] if x["elevation"] is not None else np.nan
                            for x in j["results"]]
                    break
            except Exception:
                pass
            time.sleep(8)
        else:
            out += [np.nan] * len(chunk)
        time.sleep(2)
    return np.round(pd.Series(out, index=df.index).astype(float), 0)


# -------------------------------------------------------------------- 2. indicators
R_EARTH = 6371.0088


def haversine(lat1, lon1, lat2, lon2):
    p = np.pi / 180
    a = (np.sin((lat2 - lat1) * p / 2) ** 2
         + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2)
    return 2 * R_EARTH * np.arcsin(np.sqrt(a))


def terrain_speed(elevation):
    out = np.full(len(elevation), SPEED_BANDS[-1][1], dtype=float)
    for ceiling, kmh in reversed(SPEED_BANDS):
        out = np.where(np.asarray(elevation) < ceiling, kmh, out)
    return out


UPPER_RE = re.compile(
    r"liceo|istituto tecnico|istituto professionale|istruzione superiore|i\.i\.s|\biis\b|"
    r"itis|\bipsia\b|ipseoa|istituto superiore|alberghiero|geometri|"
    r"scuola secondaria di (secondo|ii)", re.I)
NON_RAIL = {"subway", "light_rail", "funicular", "monorail"}


def poi_points(elements, predicate):
    pts = []
    for e in elements:
        t = e.get("tags", {})
        if not predicate(t):
            continue
        c = e.get("center") or ({"lat": e.get("lat"), "lon": e.get("lon")}
                                if e.get("lat") is not None else None)
        if c and c["lat"] is not None:
            pts.append((c["lat"], c["lon"]))
    return np.array(pts) if pts else np.zeros((0, 2))


def build_layers(poi):
    H, M, E, D = poi["health"], poi["mobility"], poi["education"], poi["daily"]
    hosp = lambda t: t.get("amenity") == "hospital" or t.get("healthcare") == "hospital"
    layers = {
        "er":     poi_points(H, lambda t: hosp(t) and t.get("emergency") == "yes"),
        "pharm":  poi_points(H, lambda t: t.get("amenity") == "pharmacy"),
        "doc":    poi_points(H, lambda t: t.get("amenity") in ("doctors", "clinic")),
        "vet":    poi_points(H, lambda t: t.get("amenity") == "veterinary"),
        "rail":   poi_points(M, lambda t: t.get("railway") in ("station", "halt")
                             and t.get("station") not in NON_RAIL
                             and t.get("usage") != "industrial"),
        "mjunc":  poi_points(M, lambda t: t.get("highway") == "motorway_junction"),
        "kinder": poi_points(E, lambda t: t.get("amenity") == "kindergarten"),
        "school": poi_points(E, lambda t: t.get("amenity") == "school"),
        "upper":  poi_points(E, lambda t: t.get("amenity") == "school"
                             and ("3" in str(t.get("isced:level", ""))
                                  or UPPER_RE.search(t.get("name", "") or ""))),
        "super":  poi_points(D, lambda t: t.get("shop") == "supermarket"),
        "post":   poi_points(D, lambda t: t.get("amenity") == "post_office"),
        "retail": poi_points(D, lambda t: t.get("shop") in ("supermarket", "convenience")
                             or t.get("amenity") in ("bank", "post_office", "library")),
    }
    if os.path.exists("er_override.csv"):                 # official pronto soccorso list
        ov = pd.read_csv("er_override.csv")
        layers["er"] = ov[["lat", "lon"]].to_numpy()
        print(f"  er layer overridden with {len(layers['er'])} official locations")
    return layers


def compute_indicators(com, layers):
    lat, lon = com["lat"].values[:, None], com["lon"].values[:, None]
    speed = terrain_speed(com["ele_m"].values)

    def nearest_min(pts):
        if len(pts) == 0:
            return np.full(len(com), np.nan)
        d = haversine(lat, lon, pts[:, 0][None, :], pts[:, 1][None, :]).min(axis=1)
        return np.round(d * DETOUR / speed * 60, 1)

    def count_within(pts, km):
        if len(pts) == 0:
            return np.zeros(len(com), int)
        return (haversine(lat, lon, pts[:, 0][None, :], pts[:, 1][None, :]) <= km).sum(axis=1)

    for key in ["er", "pharm", "doc", "vet", "rail", "mjunc",
                "kinder", "school", "upper", "super", "post"]:
        com["m_" + key] = nearest_min(layers[key])
    com["n_retail5"] = count_within(layers["retail"], 5)
    return com



# --------------------------------------------------------------- 2b. context layer
BOUNDARIES = ("https://raw.githubusercontent.com/openpolis/geojson-italy/master/"
              "geojson/limits_IT_municipalities.geojson")


def fetch_tourism_counts(codes):
    """Tourist accommodation establishments inside each comune (point-in-polygon)."""
    from shapely.geometry import shape, Point
    from shapely.strtree import STRtree
    kinds = ["hotel", "guest_house", "apartment", "hostel", "motel",
             "chalet", "camp_site", "caravan_site"]
    q = ("[out:json][timeout:600];("
         + "".join(f'nwr["tourism"="{k}"]({REGION_BBOX});' for k in kinds)
         + ");out tags center;")
    pois = cached("poi_tourism.json", lambda: overpass(q))
    if not os.path.exists("limits.geojson"):
        r = requests.get(BOUNDARIES, headers=HDR, timeout=600)
        open("limits.geojson", "wb").write(r.content)
    gj = json.load(open("limits.geojson"))
    polys, keys = [], []
    for f in gj["features"]:
        c = str(f["properties"]["com_istat_code"]).zfill(6)
        if c in codes:
            polys.append(shape(f["geometry"])); keys.append(c)
    tree = STRtree(polys)
    counts = {c: 0 for c in codes}
    for e in pois:
        ct = e.get("center") or ({"lat": e.get("lat"), "lon": e.get("lon")}
                                 if e.get("lat") is not None else None)
        if not ct or ct["lat"] is None:
            continue
        pt = Point(ct["lon"], ct["lat"])
        for i in tree.query(pt):
            if polys[i].contains(pt):
                counts[keys[i]] += 1
                break
    return counts


def fetch_hazard(codes):
    """ISPRA IdroGEO: share of residents inside mapped flood / landslide hazard zones."""
    if os.path.exists("hazard.json"):
        return json.load(open("hazard.json"))
    out = {}
    for n, c in enumerate(sorted(codes)):
        for _ in range(3):
            try:
                r = requests.get(f"https://idrogeo.isprambiente.it/api/pir/comuni/{int(c)}",
                                 headers=HDR, timeout=45)
                if r.status_code == 200:
                    d = r.json()
                    out[c] = dict(flood_p3=d.get("popidp3_p"), flood_p2=d.get("popidp2_p"),
                                  land_p34=d.get("popfrp3p4p"))
                    break
            except Exception:
                pass
            time.sleep(3)
        if n % 60 == 0:
            print(f"     idrogeo {n}/{len(codes)}", flush=True)
        time.sleep(.12)
    json.dump(out, open("hazard.json", "w"))
    return out


def age_structure(path, codes):
    df = pd.read_csv(path, sep=";", skiprows=1, dtype={"Codice comune": str, "Età": str})
    df["c"] = df["Codice comune"].str.zfill(6)
    df = df[df["c"].isin(codes)]
    df["age"] = pd.to_numeric(df["Età"], errors="coerce")
    total = df[df["Età"].str.strip() == "999"].groupby("c")["Totale"].sum()
    a = df[df["age"].notna() & (df["age"] < 999)]
    young = a[a["age"] <= 14].groupby("c")["Totale"].sum()
    old = a[a["age"] >= 65].groupby("c")["Totale"].sum()
    ms = a.assign(w=a["age"] * a["Totale"]).groupby("c")[["w", "Totale"]].sum()
    return total, young, old, ms["w"] / ms["Totale"]


def fetch_income(codes):
    """Taxable income per taxpayer, ISTAT 'A misura di Comune' on MEF data."""
    if not os.path.exists("ben.xlsx"):
        r = requests.get("https://www.istat.it/wp-content/uploads/2025/06/"
                         "5-Benessere-economico.xlsx", headers=HDR, timeout=300)
        open("ben.xlsx", "wb").write(r.content)
    raw = pd.read_excel("ben.xlsx", sheet_name="Tav. 2.1 Comuni", header=3)
    raw.columns = [str(c).strip() for c in raw.columns]
    ccol = [c for c in raw.columns if "Codice comune" in c][0]
    years = sorted([c for c in raw.columns if c.replace(".0", "").isdigit()], key=float)
    raw["c"] = raw[ccol].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    return raw[raw["c"].isin(codes)].set_index("c")[years[-1]]


def add_coastline(com):
    """Distance to sea and to the nearest mapped beach; coastal status from the polygons.
    In the Po delta the OSM coastline also follows lagoon shores, so Goro and Comacchio
    measure against the Sacca and the valli, not the open Adriatic."""
    from shapely.geometry import shape, LineString, Point
    from shapely.ops import unary_union, nearest_points
    q = (f'[out:json][timeout:600];(way["natural"="coastline"]({COASTLINE_BBOX});'
         f'nwr["natural"="beach"]({COASTLINE_BBOX}););out geom;')
    els = cached("coast.json", lambda: overpass(q))
    line = lambda e: [(pt["lon"], pt["lat"]) for pt in e["geometry"]]
    coast = unary_union([LineString(line(e)) for e in els
                         if e.get("tags", {}).get("natural") == "coastline"
                         and e.get("geometry") and len(e["geometry"]) > 1])
    beaches = unary_union([LineString(line(e)) if len(e["geometry"]) > 1
                           else Point(line(e)[0]) for e in els
                           if e.get("tags", {}).get("natural") == "beach" and e.get("geometry")])
    gj = json.load(open("limits.geojson"))
    poly = {str(f["properties"]["com_istat_code"]).zfill(6): shape(f["geometry"])
            for f in gj["features"]
            if str(f["properties"]["com_istat_code"]).zfill(6) in set(com["istat"])}
    km = lambda a, b, lat: float(np.hypot((a.x - b.x) * np.cos(np.radians(lat)) * 111.0,
                                          (a.y - b.y) * 111.0))
    sea, beach, coastal = [], [], []
    for r in com.itertuples():
        pt = Point(r.lon, r.lat)
        sea.append(round(km(pt, nearest_points(pt, coast)[1], r.lat), 1))
        beach.append(round(km(pt, nearest_points(pt, beaches)[1], r.lat), 1))
        g = poly.get(r.istat)
        coastal.append(int(bool(g is not None and g.distance(coast) <= COAST_TOL)))
    com["sea_km"], com["beach_km"], com["coast"] = sea, beach, coastal
    return com


def add_context(com):
    codes = set(com["istat"])
    print("     tourism ...")
    com["tour_units"] = com["istat"].map(fetch_tourism_counts(codes)).fillna(0).astype(int)
    com["tour_p1000"] = (com["tour_units"] / com["pop"].clip(lower=1) * 1000).round(1)

    print("     hazard ...")
    hz = pd.DataFrame(fetch_hazard(codes)).T
    for k in ["flood_p3", "flood_p2", "land_p34"]:
        com[k] = pd.to_numeric(com["istat"].map(hz[k]), errors="coerce").round(2)

    print("     demography ...")
    if not os.path.exists("posas19/POSAS_2019_it_Comuni.csv"):
        import zipfile, io
        z = requests.get("https://demo.istat.it/data/posas/POSAS_2019_it_Comuni.zip",
                         headers=HDR, timeout=300)
        zipfile.ZipFile(io.BytesIO(z.content)).extractall("posas19")
    t25, y25, o25, mean25 = age_structure("posas/POSAS_2025_it_Comuni.csv", codes)
    t19, _, _, _ = age_structure("posas19/POSAS_2019_it_Comuni.csv", codes)
    com["pct65"] = (com["istat"].map(o25) / com["istat"].map(t25) * 100).round(1)
    com["oldindex"] = (com["istat"].map(o25) / com["istat"].map(y25) * 100).round(0)
    com["age_mean"] = com["istat"].map(mean25).round(1)
    com["pop_trend"] = ((com["istat"].map(t25) / com["istat"].map(t19) - 1) * 100).round(2)

    print("     income ...")
    com["income"] = pd.to_numeric(com["istat"].map(fetch_income(codes)),
                                  errors="coerce").round(0)
    print("     coastline ...")
    com = add_coastline(com)
    print(f"     {int(com['coast'].sum())} coastal comuni")
    return com


# ------------------------------------------------------------------------ 3. scoring
def normalise(df):
    """ISTAT goalpost normalisation -> approx [70,130], 100 = regional median."""
    out, goalposts = pd.DataFrame(index=df.index), {}
    for i in IND:
        x = pd.to_numeric(df[i], errors="coerce")
        x = x.fillna(x.median())                          # a handful of merged comuni lack 2019 pop
        lo, hi = x.quantile(.01), x.quantile(.99)         # winsorise the long tail
        xw = x.clip(lo, hi)
        ref, delta = xw.median(), (xw.max() - xw.min()) / 2 or 1.0
        r = (xw - (ref - delta)) / (2 * delta) * 60 + 70
        if DIR[i] < 0:
            r = 200 - r                                   # invert: less time = better
        out[i] = r
        goalposts[i] = dict(lo=float(lo), hi=float(hi), ref=float(ref),
                            delta=float(delta), sign=DIR[i])
    return out, goalposts


def ampi_penalised(frame, weights=None):
    """AMPI with negative penalty. Weighted mean and weighted spread, so a zero-weight
    domain leaves the composite untouched and equal weights reproduce the plain AMPI."""
    w = pd.Series(1.0, index=frame.columns) if weights is None else \
        pd.Series(weights, dtype=float).reindex(frame.columns).fillna(0)
    if w.sum() == 0:
        return pd.Series(np.nan, index=frame.index)
    w = w / w.sum()
    mean = frame.mul(w, axis=1).sum(axis=1)
    spread = np.sqrt(frame.sub(mean, axis=0).pow(2).mul(w, axis=1).sum(axis=1))
    cv = (spread / mean).replace([np.inf, -np.inf], 0).fillna(0)
    return mean - spread * cv


def score(df, weights=None):
    norm, goalposts = normalise(df)
    res = pd.DataFrame(index=df.index)
    for dom, items in DOMAINS.items():
        res[dom] = ampi_penalised(norm[items])
    res["score"] = ampi_penalised(res[list(DOMAINS)], weights or DEFAULT_W)
    return res, norm, goalposts


# --------------------------------------------------------------------------- 4. main
def main():
    print("1/6  comuni ...")
    rows = []
    for e in fetch_comuni():
        t = e["tags"]
        code = str(t["ref:ISTAT"]).zfill(6)
        rows.append(dict(istat=code, nome=t.get("name"), prov=ER_PROV[code[:3]],
                         lat=e["center"]["lat"], lon=e["center"]["lon"]))
    com = (pd.DataFrame(rows).drop_duplicates("istat")
           .sort_values("nome").reset_index(drop=True))
    com["nome"] = com["istat"].map(fetch_istat_names()).fillna(com["nome"])
    com["pop"] = com["istat"].map(fetch_population()).fillna(0).astype(int)
    print(f"     {len(com)} comuni, {com['pop'].sum():,} residents")

    print("2/6  elevation ...")
    com["ele_m"] = fetch_elevation(com)
    com.to_csv("comuni_base.csv", index=False)

    print("3/6  services ...")
    layers = build_layers(fetch_pois())

    print("4/6  indicators ...")
    com = compute_indicators(com, layers)
    com.to_csv("indicators.csv", index=False)

    print("5/6  context (hazard, demography, tourism, income) ...")
    com = add_context(com)
    com.to_csv("indicators_plus.csv", index=False)

    print("6/6  scoring ...")
    res, norm, _ = score(com)
    out = pd.concat([com, res.round(1)], axis=1)
    out["rank"] = out["score"].rank(ascending=False, method="min").astype(int)
    out.sort_values("score", ascending=False).to_csv("scored.csv", index=False)

    payload = dict(
        indicators=IND, labels=LABELS, units=UNITS,
        domainOf={i: d for d, v in DOMAINS.items() for i in v}, domains=DOMAINS,
        core=list(CORE), optional=list(OPTIONAL),
        med={i: round(float(pd.to_numeric(com[i], errors="coerce").median()), 2) for i in IND},
        context={c: dict(label=LABELS[c], unit=UNITS[c],
                         ref=("costa" if c in COAST_REF else "regione"),
                         med=round(float(pd.to_numeric(
                             (com[com["coast"] == 1] if c in COAST_REF else com)[c],
                             errors="coerce").median()), 2)) for c in CONTEXT},
        comuni=[dict(c=r["istat"], n=r["nome"], p=r["prov"], pop=int(r["pop"]),
                     e=int(r["ele_m"]), lat=round(r["lat"], 4), lon=round(r["lon"], 4),
                     coast=int(r["coast"]),
                     raw=[None if pd.isna(r[i]) else round(float(r[i]), 2) for i in IND],
                     nz=[round(float(norm.loc[k, i]), 2) for i in IND],
                     ctx={c: (None if pd.isna(r[c]) else round(float(r[c]), 2))
                          for c in CONTEXT})
                for k, r in com.iterrows()],
    )
    json.dump(payload, open("payload.json", "w"), ensure_ascii=False, separators=(",", ":"))

    top = out.sort_values("score", ascending=False)
    print("\nbest 5 :", ", ".join(f"{r.nome} {r.score:.1f}" for r in top.head(5).itertuples()))
    print("worst 5:", ", ".join(f"{r.nome} {r.score:.1f}" for r in top.tail(5).itertuples()))
    refresh_html()
    print("\nwrote comuni_base.csv, indicators.csv, indicators_plus.csv, "
          "scored.csv, payload.json")


def refresh_html(path="indice-servizi-emilia-romagna.html"):
    """Re-inject the freshly built payload into the interactive tool, in place."""
    if not os.path.exists(path):
        return
    html = open(path, encoding="utf-8").read()
    a = html.find('<script id="data" type="application/json">')
    if a < 0:
        return
    a = html.index(">", a) + 1
    b = html.index("</script>", a)
    payload = open("payload.json", encoding="utf-8").read()
    open(path, "w", encoding="utf-8").write(html[:a] + payload + html[b:])
    print(f"     refreshed {path}")


if __name__ == "__main__":
    main()
