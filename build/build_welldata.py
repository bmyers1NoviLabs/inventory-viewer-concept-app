"""Undrilled well block: every numeric metric, quantized uint16, one buffer.

Inputs (see ../deploy_config.json — `python serve.py --refresh` downloads them):
  data/econ/undrilled_{basin}/Economics/economics_all.csv  (+ Data/data_plus.csv)
  data/Undrilled_{basin}_WellboreLocations.csv
  data/static/phase_windows/{basin}.zip     (phase-window shapefiles)
  data/static/pud_res/*.zip                 (PUD/RES polygons, FORMA-coded)

Outputs: build/out/welldata.json, build/out/basemap.json
"""
import base64, glob, gzip, json, sys, zipfile
from pathlib import Path
import numpy as np, pandas as pd
import geopandas as gpd
from shapely.geometry import LineString, Point

ROOT = Path(__file__).resolve().parents[1]
DATA, STATIC, OUT = ROOT/"data", ROOT/"data/static", ROOT/"build/out"
OUT.mkdir(parents=True, exist_ok=True)
BASINS = json.loads((ROOT/"deploy_config.json").read_text()).get("basins", ["Utica","Marcellus"])

WIN_RENAME = {"Black Oil":"Oil","Vol. Oil":"Wet Gas"}
PHASE_ORDER = ["Oil","Wet Gas","Ultra Rich Gas","Rich Gas","Lean Gas","Dry Gas","Outside windows"]
# PUD/RES polygon FORMA codes -> econ Formation names
FORMA = {"LWRMC":"Lower Marcellus","UPRMC":"Upper Marcellus",
         "PP":"Point Pleasant","UTICA":"Utica"}

def unzip_to(z, dest):
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(z) as f: f.extractall(dest)
    return dest

def shp_glob(folder):
    return sorted(glob.glob(str(folder/"**"/"*.shp"), recursive=True))

frames, windows_out = [], []
CFG = json.loads((ROOT/"deploy_config.json").read_text())
PRIMARY_DECK = str(CFG.get("price_deck") or "")

def combined_dir(basin):
    """the new one-file export (drilled + undrilled + all price decks):
       data/econ/all_{basin}.zip / {basin}_all.zip, or a pre-extracted folder.
       Returns the folder that holds Economics/economics_all.csv, else None."""
    for z in (DATA/f"econ/all_{basin}.zip", DATA/f"econ/{basin}_all.zip"):
        if z.exists():
            unzip_to(z, DATA/f"econ/all_{basin.lower()}"); break
    d = DATA/f"econ/all_{basin.lower()}"
    if not d.exists(): return None
    hits = sorted(glob.glob(str(d/"**"/"economics_all.csv"), recursive=True))
    return Path(hits[0]).parents[1] if hits else None

ALT_FRAMES = {}          # deck -> [per-basin undrilled frames] for non-primary decks
DECKS_SEEN = []

for basin in BASINS:
    comb = combined_dir(basin)
    if comb:
        e  = pd.read_csv(comb/"Economics/economics_all.csv", low_memory=False)
        dp = pd.read_csv(comb/"Data/data_plus.csv", low_memory=False)
        e["_inv_file"] = e["Inventory Type"].astype(str).str.strip()
        e = e[e["_inv_file"] != "PDP"].copy()          # undrilled = non-PDP rows
        dk = e["Price Deck"].astype(str).str.strip() if "Price Deck" in e else pd.Series("?",index=e.index)
        decks = sorted(dk.unique())
        primary = PRIMARY_DECK if PRIMARY_DECK in decks else decks[0]
        for x in decks:
            if x not in DECKS_SEEN: DECKS_SEEN.append(x)
        for x in decks:
            if x != primary:
                ALT_FRAMES.setdefault(x, []).append(e[dk == x].copy())
        e = e[dk == primary].copy()
        print(f"{basin}: combined export {comb.name} — undrilled {len(e):,} "
              f"| decks {decks} (primary {primary})")
    else:
        econ = DATA/f"econ/undrilled_{basin.lower()}"
        if not econ.exists():
            z = DATA/f"econ/undrilled_{basin}.zip"
            alt = DATA/f"econ/undrilled_{basin.lower()}.zip"
            if not (z.exists() or alt.exists()):
                print(f"{basin}: no combined export and no undrilled zip — skipping this basin")
                continue
            unzip_to(z if z.exists() else alt, econ)
        e  = pd.read_csv(econ/"Economics/economics_all.csv", low_memory=False)
        dp = pd.read_csv(econ/"Data/data_plus.csv", low_memory=False)
        e["_inv_file"] = np.nan                        # old export: classify by polygon below
    e["_k"]  = e["Unique ID"].astype(str).str.strip()
    dp["_k"] = dp["Unique ID"].astype(str).str.strip()
    e = e.merge(dp.drop(columns=["Unique ID"]), on="_k", how="left", suffixes=("", " [d+]"))

    loc = pd.read_csv(DATA/f"Undrilled_{basin}_WellboreLocations.csv", low_memory=False)
    for c in ("latitude","longitude","path"): loc[c]=pd.to_numeric(loc[c],errors="coerce")
    loc = loc.dropna(subset=["latitude","longitude","path"]).sort_values(["novi_wellname","path"])
    g = loc.groupby("novi_wellname"); heel,toe = g.first(),g.last()
    geo = pd.DataFrame({"hx":heel.longitude,"hy":heel.latitude,"tx":toe.longitude,"ty":toe.latitude})
    geo.index = geo.index.astype(str).str.strip()
    e = e.merge(geo, left_on="_k", right_index=True, how="inner")

    # phase windows: classify projected midpoints, collect outlines
    wdir = unzip_to(STATIC/f"phase_windows/{basin}.zip", OUT/f"_pw_{basin}")
    win = gpd.read_file(shp_glob(wdir)[0]).to_crs(4326)
    win["PW"] = win["Phase"].map(lambda x: WIN_RENAME.get(str(x).strip(), str(x).strip()))
    wells = gpd.GeoDataFrame({"k": e["_k"]},
        geometry=[LineString([(r.hx,r.hy),(r.tx,r.ty)]) for r in e.itertuples()], crs=4326)
    mids = wells.copy()
    mids["geometry"] = gpd.GeoSeries(
        wells.to_crs(5070).geometry.interpolate(0.5,normalized=True),crs=5070).to_crs(4326).values
    j = gpd.sjoin(mids, win[["PW","geometry"]], how="left", predicate="within").drop_duplicates("k")
    e = e.merge(j[["k","PW"]].rename(columns={"k":"_k"}), on="_k", how="left")
    e["PW"] = e["PW"].fillna("Outside windows")
    e["_mid"] = mids.geometry.values
    e["_play"] = basin
    frames.append(e)

    wsim = win.copy(); wsim["geometry"] = wsim.geometry.simplify(0.004)
    for _, r in wsim.iterrows():
        rings=[]
        geoms = r.geometry.geoms if r.geometry.geom_type=="MultiPolygon" else [r.geometry]
        for poly in geoms:
            rings.append([[round(x,4),round(y,4)] for x,y in poly.exterior.coords])
        c = r.geometry.centroid
        windows_out.append({"n":r.PW,"p":basin,"c":[round(c.x,3),round(c.y,3)],"r":rings})
    print(f"{basin}: {len(e):,} wells | phase {e.PW.value_counts().to_dict()}")

e = pd.concat(frames, ignore_index=True)
print("combined:", len(e))

# Inventory Type from the PUD/RES polygons (formation-matched; outside -> RES)
pr_frames=[]
for z in sorted((STATIC/"pud_res").glob("*.zip")):
    d=unzip_to(z, OUT/f"_pr_{z.stem}")
    for shp in shp_glob(d):
        gdf=gpd.read_file(shp).to_crs(4326)
        cols={c.lower():c for c in gdf.columns}
        fc=cols.get("forma") or cols.get("formation")
        wc=(cols.get("wellclass") or cols.get("pud/res") or cols.get("pud_res")
              or cols.get("pudres") or cols.get("class"))
        if fc is None or wc is None:
            print(f"  ! {shp}: no FORMA/WellClass columns ({list(gdf.columns)[:8]}) — skipped"); continue
        gdf["_form"]=gdf[fc].astype(str).str.strip().str.upper().map(FORMA).fillna(gdf[fc].astype(str))
        gdf["_cls"]=gdf[wc].astype(str).str.strip().str.upper()
        pr_frames.append(gdf[["_form","_cls","geometry"]])
inv = pd.Series("Emerging Inventory", index=e.index)   # outside polygon -> RES
if pr_frames:
    pr=pd.concat(pr_frames, ignore_index=True)
    pts=gpd.GeoDataFrame({"i":e.index,"form":e["Formation"].astype(str).str.strip()},
                         geometry=list(e["_mid"]), crs=4326)
    hit=gpd.sjoin(pts, gpd.GeoDataFrame(pr, crs=4326), how="left", predicate="within")
    hit=hit[hit["_form"]==hit["form"]].drop_duplicates("i")
    m=hit.set_index("i")["_cls"]
    inv.loc[m.index]=np.where(m.str.contains("PUD"),"Base Case","Emerging Inventory")
# the combined export names its own inventory class per row — trust it there;
# the polygon classification only fills in for old-format basins
fromfile = e["_inv_file"].notna() & (e["_inv_file"].astype(str)!="nan")
e["Inventory Type"] = np.where(fromfile, e["_inv_file"].astype(str), inv.values)
print("inventory:", e.groupby("_play")["Inventory Type"].value_counts().to_dict())
e=e.drop(columns=["_mid","_inv_file"])

bm = json.loads((ROOT/"app/assets/basemap.json").read_text())
bm["windows"] = windows_out
(OUT/"basemap.json").write_text(json.dumps(bm,separators=(",",":")))

EXCLUDE = {"hx","hy","tx","ty","Surface Hole Latitude","Surface Hole Longitude"}
GROUP_RULES = [
    ("Breakevens",  lambda c: "Breakeven" in c),
    ("NPV & value", lambda c: ("NPV" in c or "Value" in c or "PVI" in c) and "Breakeven" not in c),
    ("Returns & payout", lambda c: "IRR" in c or "Pay Out" in c or "Cash Flow" in c),
    ("EURs",        lambda c: "EUR" in c.upper()),
    ("IP rates",    lambda c: " IP" in c or c.startswith("IP")),
    ("Cumulatives & % produced", lambda c: "Cumulative" in c or "Percent of" in c),
    ("Costs",       lambda c: "Cost" in c or "($)" in c or "$/" in c or c in
        ("Drill Speed","Frac Speed","Stage Spacing","Drilling Days","Estimated Stages","Pumping Days")),
    ("NGL & gas quality", lambda c: "NGL" in c or "Shrink" in c),
    ("Prices & fiscal", lambda c: c.startswith("Avg_") or "Differential" in c
        or "Royalty" in c or "Tax" in c),
    ("Well & completion", lambda c: c in ("TVD","MD","Lateral Length","First Production Year")
        or "Proppant" in c or "Fluid" in c or "Spacing Azimuth" in c),
    ("Geology",     lambda c: any(k in c for k in ("Porosity","Permeability","Thickness","TOC",
        "Vclay","Sw ","HCPV","BVI","Area","Depletion","Prior","Child"))),
    ("Model scores",lambda c: "Score" in c or "Uncertainty" in c or "Distance to" in c or "Within" in c),
]
def group_of(c):
    for gname, rule in GROUP_RULES:
        if rule(c): return gname
    return "Other"

metrics, groups, qmeta, chunks = [], {}, {}, []
for col in e.columns:
    if col.startswith("PLSS") or col in EXCLUDE or col in ("_k","_play","PW"): continue
    v = pd.to_numeric(e[col], errors="coerce")
    ok = v.notna().mean()
    if ok < 0.3 or (e[col].dtype==object and ok<0.9): continue
    arr = v.to_numpy(dtype=np.float64)
    fin = arr[np.isfinite(arr)]
    if fin.size==0 or np.nanmax(fin)==np.nanmin(fin): continue
    lo, hi = float(fin.min()), float(fin.max())
    scale = (hi-lo)/65534.0
    q = np.full(arr.shape, 65535, dtype=np.uint16)
    m = np.isfinite(arr)
    q[m] = np.clip(np.round((arr[m]-lo)/scale),0,65534).astype(np.uint16)
    qmeta[col]=[lo,scale]; chunks.append(q); metrics.append(col)
    groups.setdefault(group_of(col),[]).append(col)
blob = base64.b64encode(gzip.compress(np.concatenate(chunks).tobytes(),6)).decode()
print(f"metrics kept {len(metrics)}")

# ── alternate price decks: same wells, same metric list, one quantized blob
#    per deck. Columns a deck's rows don't carry (deck-independent geology,
#    completion, etc.) fall back to the primary deck's values. ──
altdecks = {}
for dkname, frames in ALT_FRAMES.items():
    a = pd.concat(frames, ignore_index=True)
    a["_k"] = a["Unique ID"].astype(str).str.strip()
    a = a.drop_duplicates("_k").set_index("_k")
    order = e["_k"]
    present = order.isin(a.index).to_numpy()   # wells this deck actually re-scored
    aq = []; ameta = {}
    for col in metrics:
        prim = pd.to_numeric(e[col], errors="coerce").to_numpy(dtype=np.float64)
        if col in a.columns:
            v = pd.to_numeric(a[col], errors="coerce").reindex(order).to_numpy(dtype=np.float64)
            v[~present] = prim[~present]       # basins without this deck keep primary econ
        else:
            v = prim                            # deck-independent column
        fin = v[np.isfinite(v)]
        lo = float(fin.min()) if fin.size else 0.0
        hi = float(fin.max()) if fin.size else 0.0
        scale = (hi-lo)/65534.0 or 1.0
        q = np.full(v.shape, 65535, dtype=np.uint16)
        m = np.isfinite(v)
        q[m] = np.clip(np.round((v[m]-lo)/scale), 0, 65534).astype(np.uint16)
        ameta[col] = [lo, scale]; aq.append(q)
    altdecks[dkname] = {"qmeta": ameta,
        "qblob": base64.b64encode(gzip.compress(np.concatenate(aq).tobytes(),6)).decode()}
    print(f"alt price deck {dkname}: {len(a):,} rows baked over {len(metrics)} metrics")

def cat(series):
    vals = series.astype(str).str.strip().fillna("")
    levels = sorted(vals.unique())
    return {"levels":levels,"ix":[levels.index(v) for v in vals]}
phase_levels = [p for p in PHASE_ORDER if p in set(e.PW)]

ph = e["Primary Hydrocarbon"].astype(str)
use_oil = ph.str.contains("Oil|Liquids", case=False, na=False)
e["_rqq"] = np.where(use_oil, e["ML-Derived Rock Quality Quartile (Oil)"].astype(str),
                              e["ML-Derived Rock Quality Quartile (Gas)"].astype(str))
e.loc[~e["_rqq"].isin(["Tier-1","Tier-2","Tier-3","Tier-4"]), "_rqq"] = "Unknown"
def ordcat(series, order):
    levels=[l for l in order if l in set(series)]
    return {"levels":levels,"ix":[levels.index(v) for v in series]}
TIERS=["Tier-1","Tier-2","Tier-3","Tier-4","Unknown"]

import datetime
primary_label = str(e["Price Deck"].iloc[0]).strip() if "Price Deck" in e else "?"
data = {
  "play":"Appalachia","generated":datetime.date.today().isoformat(),
  "priceDeck":primary_label,
  "decks":[primary_label]+[d0 for d0 in DECKS_SEEN if d0!=primary_label],
  "n":len(e), "id": e["_k"].tolist(),
  "geo": [[round(a,5) for a in row] for row in e[["hx","hy","tx","ty"]].to_numpy()],
  "cats": {
    "Play": cat(e["_play"]), "Formation": cat(e["Formation"]),
    "County": cat(e["County"]), "State": cat(e["State"]),
    "Phase": {"levels":phase_levels,"ix":[phase_levels.index(p) for p in e.PW]},
    "Operator": cat(e["Operator"]), "Subbasin": cat(e["Subbasin"]),
    "Inventory": cat(e["Inventory Type"]),
    "IRRTier": {"levels":TIERS,"ix":[4]*len(e)},   # rebuilt in-app from PUD+PDP IRR
    "RQQ": ordcat(e["_rqq"], TIERS),
  },
  "metricGroups": [[g, groups[g]] for g,_ in GROUP_RULES if g in groups]
                  + ([["Other",groups["Other"]]] if "Other" in groups else []),
  "metrics": metrics, "qmeta": qmeta, "qblob": blob,
}
if altdecks: data["altdecks"]=altdecks
(OUT/"welldata.json").write_text(json.dumps(data, separators=(",",":")))
print(f"welldata.json {(OUT/'welldata.json').stat().st_size/1e6:.1f} MB")
