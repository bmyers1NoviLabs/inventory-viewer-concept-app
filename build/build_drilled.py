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

# ── phase windows: same shapefiles + rename + methodology as build_welldata,
#    so drilled and undrilled wells share one phase-window vocabulary ──
WIN_RENAME = {"Black Oil":"Oil","Vol. Oil":"Wet Gas"}
_WINDOWS = {}
try:
    import geopandas as gpd
    from shapely.geometry import Point
    for _b in BASINS:
        _z = ROOT/"data/static/phase_windows"/f"{_b}.zip"
        _wd = _unzip(_z, OUT/f"_pw_dr_{_b}")
        import glob as _glob
        _shp = sorted(_glob.glob(str(_wd/"**"/"*.shp"), recursive=True))[0]
        _w = gpd.read_file(_shp).to_crs(4326)
        _w["PW"] = _w["Phase"].map(lambda x: WIN_RENAME.get(str(x).strip(), str(x).strip()))
        _WINDOWS[_b] = _w[["PW","geometry"]]
except Exception as _e:
    print(f"phase windows unavailable ({_e}) — Phase grouping will be Unknown")
    _WINDOWS = {}

def classify_pw(lons, lats, plays):
    """point-in-window classification per play; Unknown play or no windows -> Unknown"""
    out = ["Unknown"]*len(lons)
    if not _WINDOWS: return out
    pts = gpd.GeoDataFrame({"i": range(len(lons)), "play": list(plays)},
        geometry=[Point(x,y) for x,y in zip(lons,lats)], crs=4326)
    for b, win in _WINDOWS.items():
        sub = pts[pts["play"]==b]
        if sub.empty: continue
        j = gpd.sjoin(sub, win, how="left", predicate="within").drop_duplicates("i")
        for i, pw in zip(j["i"], j["PW"]):
            out[i] = pw if isinstance(pw,str) else "Outside windows"
    return out

# ── drilled stick geometry from the PDP WellboreLocations.tsv: heel->toe per
#    API10, matched to the deck's drilled wells, drawn on the map as the grey
#    "Drilled PDP sticks" layer. Column names resolved case-insensitively. ──
import re as _re0
_WBL = DATA/"WellboreLocations.tsv"
if not _WBL.exists():
    print("WellboreLocations.tsv not found — drilled map sticks SKIPPED")
else:
    _wcols = pd.read_csv(_WBL, sep="\t", nrows=0).columns.tolist()
    def _wfind(*pats):
        for p in pats:
            for c in _wcols:
                if _re0.fullmatch(p, c.strip(), _re0.I): return c
        return None
    _w_id  = _wfind(r"api.?10", r"api", r"uwi", r"novi.?wellname", r"unique.?id")
    _w_lat = _wfind(r"latitude", r"lat")
    _w_lon = _wfind(r"longitude", r"lon(g)?")
    _w_pth = _wfind(r"path", r"md", r"measured.?depth", r"point.?(order|seq\w*)", r"sequence")
    if not (_w_id and _w_lat and _w_lon):
        print(f"WellboreLocations pace columns missing (id={_w_id} lat={_w_lat} lon={_w_lon}); "
              f"available: {_wcols} — drilled map sticks SKIPPED")
    else:
        _use=[c for c in {_w_id,_w_lat,_w_lon,_w_pth} if c]
        wl = pd.read_csv(_WBL, sep="\t", usecols=_use, dtype={_w_id:str})
        wl[_w_lat]=pd.to_numeric(wl[_w_lat],errors="coerce")
        wl[_w_lon]=pd.to_numeric(wl[_w_lon],errors="coerce")
        wl = wl.dropna(subset=[_w_lat,_w_lon])
        wl["_k"]=wl[_w_id].astype(str).str.strip()
        keep=set(d["_k"])
        wl = wl[wl["_k"].isin(keep)]                      # only the deck's drilled wells
        if _w_pth:
            wl[_w_pth]=pd.to_numeric(wl[_w_pth],errors="coerce")
            wl = wl.dropna(subset=[_w_pth]).sort_values(["_k",_w_pth])
        g = wl.groupby("_k",sort=False)
        heel, toe = g.first(), g.last()
        gm = {k:[round(float(heel[_w_lon][k]),5),round(float(heel[_w_lat][k]),5),
                 round(float(toe[_w_lon][k]),5), round(float(toe[_w_lat][k]),5)] for k in heel.index}
        geo=[gm.get(k) for k in d["_k"]]
        nhit=sum(1 for x in geo if x)
        D["drilled"]["geo"]=geo
        print(f"drilled sticks: {nhit:,} of {len(d):,} deck wells matched in WellboreLocations "
              f"(path col: {_w_pth or 'none — file order'})")
        # phase window per drilled well from the stick midpoint (Quick-Eval grouping)
        _mx=[(g[0]+g[2])/2 if g else 0.0 for g in geo]
        _my=[(g[1]+g[3])/2 if g else 0.0 for g in geo]
        _pw=classify_pw(_mx,_my,d["_play"].tolist())
        _pw=[p if geo[i] else "Unknown" for i,p in enumerate(_pw)]
        D["drilled"]["cats"]["Phase"]=cat(pd.Series(_pw))
        from collections import Counter as _Ctr
        print(f"drilled phase windows: {dict(_Ctr(_pw))}")

# ── drilling pace from WellDetails.tsv: horizontal, non-permit wells bucketed
#    by first-production HALF-YEAR. Fuels the Quick-Eval schedule prefill —
#    the page picks the last full half (9 months back), annualizes it (x2)
#    and spreads it across the schedule columns. Column names are resolved
#    case-insensitively so export renames don't break the build. ──
import re as _re
_hdr = pd.read_csv(DATA/"WellDetails.tsv", sep="\t", nrows=0).columns.tolist()
def _find(*pats):
    for p in pats:
        for c in _hdr:
            if _re.fullmatch(p, c.strip(), _re.I): return c
    return None
_c = {
  "fpd":    _find(r"first.?production.?date", r".*first.?prod\w*.?date.*"),
  "hz":     _find(r"is.?horizontal.?well", r".*is.?horizontal.*", r"hole.?direction"),
  "status": _find(r"status", r".*well.?status.*", r".*current.?status.*"),
  "op":     _find(r"current.?operator", r"operator"),
  "state":  _find(r"state"),
  "county": _find(r"county"),
  "form":   _find(r"formation"),
}
_c_shla = _find(r"shl.?latitude", r"surface.?hole.?latitude", r"surface.?latitude")
_c_shlo = _find(r"shl.?longitude", r"surface.?hole.?longitude", r"surface.?longitude")
_missing = [k for k,v in _c.items() if v is None]
if _missing:
    print(f"WellDetails pace SKIPPED — no column for {_missing}; available: {_hdr}")
else:
    _use = list(set(_c.values()))+[c for c in (_c_shla,_c_shlo) if c]
    wp = pd.read_csv(DATA/"WellDetails.tsv", sep="\t", dtype=str, usecols=_use)
    n0 = len(wp)
    hz = wp[_c["hz"]].astype(str).str.strip().str.lower()
    wp = wp[hz.isin(["t","true","1","y","yes","horizontal","h"])]
    wp = wp[~wp[_c["status"]].astype(str).str.contains("permit", case=False, na=False)]
    fpd = pd.to_datetime(wp[_c["fpd"]], errors="coerce")
    wp = wp[fpd.notna()]; fpd = fpd[fpd.notna()]
    hk = fpd.dt.year.astype(int)*2 + (fpd.dt.month > 6).astype(int)   # half-year key
    keep = sorted(set(hk))[-6:]                                       # trailing 6 halves
    m = hk.isin(keep)
    wp, hk = wp[m], hk[m]
    fu = wp[_c["form"]].fillna("").astype(str).str.strip().str.upper()
    hlab = lambda k: f"{k//2}H{k%2+1}"
    tbl = pd.DataFrame({
        "op":     wp[_c["op"]].fillna("Unknown").astype(str).str.strip(),
        "play":   ["Marcellus" if f in MARC_F else "Utica" if f in UTICA_F else "Unknown" for f in fu],
        "state":  wp[_c["state"]].fillna("Unknown").astype(str).str.strip(),
        "county": wp[_c["county"]].fillna("Unknown").astype(str).str.strip(),
        "h":      hk.values})
    # phase window from the surface hole location (pace prefill for PW groupings)
    if _c_shla and _c_shlo:
        _la=pd.to_numeric(wp[_c_shla],errors="coerce").fillna(0).tolist()
        _lo=pd.to_numeric(wp[_c_shlo],errors="coerce").fillna(0).tolist()
        tbl["pw"]=classify_pw(_lo,_la,tbl["play"].tolist())
    else:
        tbl["pw"]="Unknown"
        print("WellDetails has no SHL lat/lon — pace phase windows set to Unknown")
    hpos = {k:i for i,k in enumerate(keep)}
    rows = {}
    for (op,pl,st,co,pw,hh),n in tbl.groupby(["op","play","state","county","pw","h"]).size().items():
        rows.setdefault((op,pl,st,co,pw),[0]*len(keep))[hpos[hh]] = int(n)
    D["pace"] = {"halves":[hlab(k) for k in keep],
                 "rows":[[op,pl,st,co,pw,c] for (op,pl,st,co,pw),c in sorted(rows.items())]}
    print(f"drilling pace: {int(m.sum()):,} of {n0:,} WellDetails wells "
          f"(horizontal, non-permit, FPD {hlab(keep[0])}-{hlab(keep[-1])}) -> {len(rows):,} pace groups")

s = json.dumps(D, separators=(",",":"))
open(OUT/"welldata.json","w").write(s)
open(OUT/"welldata.gz.b64","w").write(base64.b64encode(gzip.compress(s.encode(),6)).decode())
print(f"welldata.json {len(s)/1e6:.1f} MB | gz.b64 {(OUT/'welldata.gz.b64').stat().st_size/1e6:.1f} MB")
