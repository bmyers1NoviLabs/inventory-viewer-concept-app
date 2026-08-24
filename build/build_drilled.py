"""Drilled (PDP) wells from the 2026_08_22 calc runs -> compact D["drilled"]
block for the Drilled-vs-Undrilled chart.

Each run scores every drilled well in its AOI, so the two files overlap
(the Utica run carries Marcellus-formation wells and vice versa). A well is
taken from the run that matches its formation, then the union is deduped on
Unique ID.
"""
import base64, glob, gzip, json, zipfile
from pathlib import Path
import numpy as np, pandas as pd
ROOT = Path(__file__).resolve().parents[1]
DATA, OUT = ROOT/"data", ROOT/"build/out"
BASINS = json.loads((ROOT/"deploy_config.json").read_text()).get("basins",["Utica","Marcellus"])
def _unzip(z,d):
    d.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(z) as f: f.extractall(d)
    return d

MARC_F = {"LOWER MARCELLUS":"Lower Marcellus","UPPER MARCELLUS":"Upper Marcellus",
          "MARCELLUS SHALE":"Marcellus Shale","MARCELLUS BASE":"Marcellus Shale",
          "GENESEO BURKET":"Geneseo Burket","UNION SPRINGS":"Union Springs"}
UTICA_F = {"POINT PLEASANT":"Point Pleasant","UTICA":"Utica",
           "POINT PLEASANT,UTICA":"Point Pleasant","UTICA-TRENTON":"Utica",
           "POINT PLEASANT,TRENTON GROUP":"Point Pleasant"}

CATS_USE = ["Unique ID","Operator","Formation","County","State","Subbasin","Phase"]
# curated drilled metric set for the DvU / arrow / trends charts (~60 columns)
PRODUCTS = ["Oil","Gas","Wet Gas","NGL","Three Stream BOE 20:1"]
METRICS = (
  [f"Total NPV{k}" for k in (0,5,10,15,20,25)]
  + ["IRR (%)","PVI0","PVI5","PVI10","PVI15","PVI20","PVI25",
     "Pay Out Period (Months)","Time To Pay Out Twice"]
  + [f"{p} {y} Year Breakeven" for p in PRODUCTS for y in ("One","Two","Three")]
  + [f"{p} NPV{k} Breakeven" for p in PRODUCTS for k in (10,25)]
  + ["Oil EUR (bbl)","Gas Eur (Mcf)","Dry Gas EUR (Mcf)","NGL EUR (bbl)","Water EUR (bbl)",
     "Two Stream EUR (boe 6:1)","Three Stream EUR (boe 6:1)",
     "Oil IP (bbl/d)","Gas IP (Mcf/d)","Dry Gas IP (Mcf/d)","NGL IP (bbl/d)","Water IP (bbl/d)",
     "Total D&C Cost ($)","Total DCET Cost ($)",
     "Normalized D&C Cost ($/ft)","Normalized DCET Cost ($/ft)",
     "NGL Yield (bbl/MMcf)","NGL Shrink","TVD","MD","Lateral Length",
     "Proppant Loading (Lbs/ft)","Fluid Loading (Gal/ft)","First Production Year"]
)
USE = CATS_USE + METRICS

def load(path, run_play):
    e = pd.read_csv(path, low_memory=False,
                    usecols=lambda c: c in USE)
    e["_k"] = e["Unique ID"].astype(str).str.strip()
    fu = e["Formation"].astype(str).str.strip().str.upper()
    e["_form"] = [MARC_F.get(f) or UTICA_F.get(f) or
                  (f.title() if f not in ("UNKNOWN","NAN") else "Unknown") for f in fu]
    e["_play"] = ["Marcellus" if f in MARC_F else
                  "Utica" if f in UTICA_F else run_play for f in fu]
    e["_match"] = [(f in MARC_F and run_play=="Marcellus") or
                   (f in UTICA_F and run_play=="Utica") for f in fu]
    return e

parts=[]
for b in BASINS:
    econ=DATA/f"econ/drilled_{b.lower()}"
    if not econ.exists():
        z=DATA/f"econ/drilled_{b}.zip"; alt=DATA/f"econ/drilled_{b.lower()}.zip"
        _unzip(z if z.exists() else alt, econ)
    parts.append(load(econ/"Economics/economics_all.csv", b))
d = pd.concat(parts, ignore_index=True)
# formation-matched run wins, then dedupe on well id
d = d.sort_values("_match", ascending=False).drop_duplicates("_k", keep="first")

# operator correction: WellDetails.tsv CurrentOperator, left-merged on API10
wd = pd.read_csv(DATA/"WellDetails.tsv", sep="\t", dtype=str,
                 usecols=["API10","CurrentOperator"]).dropna(subset=["CurrentOperator"])
wd["API10"] = wd["API10"].str.strip()
opmap = dict(zip(wd["API10"], wd["CurrentOperator"].str.strip()))
fixed = d["_k"].map(opmap)
n_fix = int((fixed.notna() & (fixed != d["Operator"].astype(str))).sum())
d["Operator"] = fixed.fillna(d["Operator"])
print(f"operator corrected from WellDetails on {n_fix:,} of {len(d):,} drilled wells")
print(f"{' + '.join(f'{b} run {len(x):,}' for b, x in zip(BASINS, parts))} -> unique drilled {len(d):,}")
print("play:", d["_play"].value_counts().to_dict())
print("formations:", d["_form"].value_counts().head(8).to_dict())

def cat(series):
    vals = series.fillna("Unknown").astype(str).str.strip().replace({"nan":"Unknown","":"Unknown"})
    levels = sorted(vals.unique())
    return {"levels":levels,"ix":[levels.index(v) for v in vals]}

lat = pd.to_numeric(d["Lateral Length"], errors="coerce").fillna(0).clip(0)

# quantized drilled metric block (same encoding as the main well block)
metrics, qmeta, chunks = [], {}, []
for c in METRICS:
    if c not in d.columns: continue
    v = pd.to_numeric(d[c], errors="coerce").to_numpy(dtype=np.float64)
    fin = v[np.isfinite(v)]
    if fin.size < len(d)*0.05 or fin.size==0 or fin.max()==fin.min(): continue
    lo,hi = float(fin.min()), float(fin.max())
    scale=(hi-lo)/65534.0
    q=np.full(v.shape,65535,dtype=np.uint16)
    msk=np.isfinite(v)
    q[msk]=np.clip(np.round((v[msk]-lo)/scale),0,65534).astype(np.uint16)
    qmeta[c]=[lo,scale]; chunks.append(q); metrics.append(c)
blob=base64.b64encode(gzip.compress(np.concatenate(chunks).tobytes(),6)).decode()
print(f"drilled metrics {len(metrics)} | blob {len(blob)/1e6:.2f} MB")

D = json.load(open(OUT/"welldata.json"))
D["drilled"] = {
  "n": len(d),
  "ids": d["_k"].tolist(),      # API10s — lets the page match any wedge source

  "cats": {
    "Play": cat(d["_play"]), "Formation": cat(d["_form"]),
    "County": cat(d["County"]), "State": cat(d["State"]),
    "Operator": cat(d["Operator"]), "Subbasin": cat(d["Subbasin"]),
  },
  "lat": [int(round(x)) for x in lat],
  "metrics": metrics, "qmeta": qmeta, "qblob": blob,
}
open(OUT/"drilled_api_order.txt","w").write("\n".join(d["_k"]))  # wedge matrix row order
s = json.dumps(D, separators=(",",":"))
open(OUT/"welldata.json","w").write(s)
open(OUT/"welldata.gz.b64","w").write(base64.b64encode(gzip.compress(s.encode(),6)).decode())
print(f"welldata.json {len(s)/1e6:.1f} MB | gz.b64 {(OUT/'welldata.gz.b64').stat().st_size/1e6:.1f} MB")
