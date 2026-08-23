"""Pad layer: join pad_economics (both plays) to the pad polygons and append a
quantized pad-metric block to welldata3.json.

Sum vs average follows pad_economics' own convention:
  Total */Sum * = summed values & volumes (NPVs, EURs, costs, proppant)
  Average */Avg * = per-well intensities (IRR, breakevens, payout, yields, TVD)
"""
import base64, gzip, json, glob
import numpy as np, pandas as pd, geopandas as gpd

from pathlib import Path
import zipfile as _zf
ROOT = Path(__file__).resolve().parents[1]
DATA, STATIC, OUT = ROOT/"data", ROOT/"data/static", ROOT/"build/out"
def _unzip(z,d):
    d.mkdir(parents=True,exist_ok=True)
    with _zf.ZipFile(z) as f: f.extractall(d)
    return d
import json as _json
BASINS=_json.loads((ROOT/"deploy_config.json").read_text()).get("basins",["Utica","Marcellus"])
PLAYS={}
for b in BASINS:
    pdir=_unzip(next(iter(sorted((STATIC/"pads").glob(f"{b}*.zip")))), OUT/f"_pads_{b}")
    PLAYS[b]=(str(DATA/f"econ/undrilled_{b.lower()}/Economics/pad_economics.csv"),
              str(pdir/"**"/"*.shp"))

frames = []
for play,(csv,shp) in PLAYS.items():
    pe = pd.read_csv(csv, low_memory=False)
    pe["_k"] = pe["Pad Name"].astype(str).str.strip()
    g = gpd.read_file(sorted(glob.glob(shp, recursive=True))[0]).to_crs(4326)
    g["_k"] = g["padName"].astype(str).str.strip()
    g = g.drop_duplicates("_k")
    m = pe.merge(g[["_k","geometry"]], on="_k", how="inner")
    print(f"{play}: econ {len(pe):,} | polygons {len(g):,} | joined {len(m):,} "
          f"| econ w/o polygon {len(pe)-len(m):,}")
    m["_play"] = play
    frames.append(m)
p = pd.concat(frames, ignore_index=True)
p = gpd.GeoDataFrame(p, geometry="geometry", crs=4326)
print("pads total:", len(p))

# metric selection straight from pad_economics' sum/avg naming
COLS = (
  ["Well Count"]
  + [f"Total NPV{k}" for k in (0,5,10,15,20,25)]
  + [f"Sum Econ NPV{k}" for k in (0,5,10,15,20,25)]
  + ["Average IRR (%)","Average Pay Out Period (Months)","Average Time To Pay Out Twice",
     "Average F&D Cost per BOE"]
  + [f"Average PVI{k}" for k in (0,5,10,15,20,25)]
  + [c for c in p.columns if c.startswith("Avg ") and "Breakeven" in c]      # all 45
  + ["Total Oil EUR (bbl)","Total Gas Eur (Mcf)","Total Dry Gas EUR (Mcf)",
     "Total NGL EUR (bbl)","Total Water EUR (bbl)",
     "Total Two Stream EUR (boe 6:1)","Total Three Stream EUR (boe 6:1)"]
  + ["Average Oil IP (bbl/d)","Average Gas IP (Mcf/d)","Average Dry Gas IP (Mcf/d)",
     "Average NGL IP (bbl/d)","Average Water IP (bbl/d)"]
  + ["Total Total D&C Cost ($)","Average Total D&C Cost ($)",
     "Total Total DCET Cost ($)","Average Total DCET Cost ($)"]
  + ["Average NGL Yield (bbl/MMcf)","Average NGL Shrink","Average TVD","Average MD",
     "Average Lateral Length","Average Proppant Loading (Lbs/ft)","Average Fluid Loading (Gal/ft)",
     "Total Proppant Remaining (lbs)","Total Fluid Remaining (gal)"]
)
COLS = [c for c in COLS if c in p.columns]
GROUPS = [
  ("Pad size", lambda c: c=="Well Count" or "Proppant Remaining" in c or "Fluid Remaining" in c),
  ("Remaining value (sums)", lambda c: c.startswith("Total NPV") or c.startswith("Sum Econ")),
  ("Returns & payout (avgs)", lambda c: "IRR" in c or "Pay Out" in c or "PVI" in c or "F&D" in c),
  ("Breakevens (avgs)", lambda c: "Breakeven" in c),
  ("EURs (sums)", lambda c: "EUR" in c),
  ("IP rates (avgs)", lambda c: " IP " in c),
  ("Costs", lambda c: "Cost" in c),
  ("Well & gas quality (avgs)", lambda c: True),
]
def grp(c):
    for g,f in GROUPS:
        if f(c): return g
metrics, groups, qmeta, chunks = [], {}, {}, []
for c in COLS:
    v = pd.to_numeric(p[c], errors="coerce").to_numpy(dtype=np.float64)
    fin = v[np.isfinite(v)]
    if fin.size==0 or fin.max()==fin.min(): continue
    lo,hi = float(fin.min()), float(fin.max())
    scale=(hi-lo)/65534.0
    q=np.full(v.shape,65535,dtype=np.uint16)
    msk=np.isfinite(v)
    q[msk]=np.clip(np.round((v[msk]-lo)/scale),0,65534).astype(np.uint16)
    qmeta[c]=[lo,scale]; chunks.append(q); metrics.append(c)
    groups.setdefault(grp(c),[]).append(c)
blob=base64.b64encode(gzip.compress(np.concatenate(chunks).tobytes(),6)).decode()
print(f"pad metrics {len(metrics)} | blob {len(blob)/1e6:.2f} MB")

# geometry: exterior rings, lightly simplified
p["geometry"]=p.geometry.simplify(0.0004)
rings=[]; cents=[]
for geom in p.geometry:
    poly = max(geom.geoms, key=lambda g:g.area) if geom.geom_type=="MultiPolygon" else geom
    rings.append([[round(x,4),round(y,4)] for x,y in poly.exterior.coords])
    c=poly.centroid; cents.append([round(c.x,4),round(c.y,4)])

def cat(series):
    vals=series.astype(str).str.strip().fillna("")
    levels=sorted(vals.unique())
    return {"levels":levels,"ix":[levels.index(v) for v in vals]}

D=json.load(open(OUT/"welldata.json"))
D["pads"]={
  "n":len(p), "name":p["_k"].tolist(),
  "rings":rings, "c":cents,
  "cats":{
    "Play":cat(p["_play"]), "County":cat(p["County"]), "State":cat(p["State"]),
    "Operator":cat(p["Operator"]), "Subbasin":cat(p["Subbasin"]),
  },
  "metricGroups":[[g,groups[g]] for g,_ in GROUPS if g in groups],
  "metrics":metrics,"qmeta":qmeta,"qblob":blob,
}
s=json.dumps(D,separators=(",",":"))
open(OUT/"welldata.json","w").write(s)
print(f"welldata.json now {len(s)/1e6:.1f} MB")
