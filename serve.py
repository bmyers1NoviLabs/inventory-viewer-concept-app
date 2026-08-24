#!/usr/bin/env python3
"""Tiny server for the Appalachia Inventory Dashboard on an EC2 box.

    python3 serve.py [port]           (default 8080)

No dependencies — Python 3 stdlib only. Run it in the folder that contains
"Appalachia Inventory Dashboard.html". Behind it the dashboard gains:

  GET  /               -> the dashboard HTML
  GET  /api/config     -> config.json   (the saved filter setup; 404-safe)
  POST /api/config     -> writes config.json  (the page calls this on every
                          filter change — settings now live on the server,
                          not in one browser's localStorage)
  POST /api/export     -> body {"name": "...", "content": "..."} or
                          {"name": "...", "content_b64": "..."} for binary.
                          Saves under ./exports/ and returns {"url": ...}.
                          This is the hook for Quick-Eval run outputs.
  GET  /exports/<file> -> download a saved export

To keep it running:  nohup python3 serve.py 8080 &   (or a systemd unit).
Put nginx or an ALB in front if you want TLS / auth.
"""
import array, base64, glob, gzip, io, json, os, re, struct, sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
PROJECTS = ROOT / "projects.json"   # named filter sets saved from the ⚙ Projects menu
EXPORTS = ROOT / "exports"
DASH = next(iter(sorted(ROOT.glob("*.html"))), None)

SAFE = re.compile(r"[^A-Za-z0-9._ ()-]")

# ── S3 sync (EC2 deploy) ─────────────────────────────────────────────────────
# deploy_config.json, next to this script, names the basins and where each
# source file lives on S3. Patterns support:
#   {basin}          -> each entry in "basins"
#   {latest:PATTERN} -> the newest S3 "folder" matching PATTERN (their
#                       timestamped export prefixes sort correctly by name),
#                       PATTERN itself may contain {basin} and * wildcards
# Files already present locally are not re-downloaded (pass --refresh to force).
# Auth is the standard boto3 chain — on EC2, give the instance an IAM role
# with s3:GetObject + s3:ListBucket on the buckets involved.
CONFIG_S3 = ROOT / "deploy_config.json"

def _s3_client(region):
    import boto3
    return boto3.client("s3", region_name=region)

def _resolve_match(s3, bucket, pattern):
    """Newest object in `bucket` whose key matches glob `pattern`."""
    import fnmatch
    pre = pattern.split("*")[0]
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=pre)
    best = None
    for page in pages:
        for o in page.get("Contents", []):
            if fnmatch.fnmatch(o["Key"], pattern):
                if best is None or o["LastModified"] > best[1]:
                    best = (o["Key"], o["LastModified"])
    if best is None:
        raise FileNotFoundError(f"no s3://{bucket}/ object matches {pattern!r}")
    return best[0]

def _resolve_latest(s3, bucket, pattern):
    """Newest top-level prefix in `bucket` matching glob `pattern`."""
    import fnmatch
    pre = pattern.split("*")[0].split("{")[0]
    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=pre, Delimiter="/")
    hits = []
    for page in pages:
        for cp in page.get("CommonPrefixes", []):
            name = cp["Prefix"].rstrip("/")
            if fnmatch.fnmatch(name, pattern):
                hits.append(name)
    if not hits:
        raise FileNotFoundError(f"no s3://{bucket}/ prefix matches {pattern!r}")
    return sorted(hits)[-1]          # timestamped names sort chronologically

def sync_s3(refresh=False):
    if not CONFIG_S3.exists():
        return
    cfg = json.loads(CONFIG_S3.read_text())
    region = cfg.get("region", "us-east-2")
    basins = cfg.get("basins", [""])
    try:
        s3 = _s3_client(region)
    except ImportError:
        print("S3 sync skipped: boto3 not installed (pip install boto3)")
        return
    latest_cache = {}
    missing = []
    for src in cfg.get("sources", []):
        per_basin = "{basin}" in src["local"] or "{basin}" in src["s3"]
        for basin in (basins if per_basin else [""]):
            local = ROOT / src["local"].replace("{basin}", basin)
            # A copy sitting in a local_dirs folder is a dev-machine convenience
            # and is used as-is; on a server there are no local_dirs, so every
            # source is checked against S3 below.
            if not local.exists() and _find_src(
                    Path(src["local"].replace("{basin}", basin)).name).exists():
                continue
            uri = src["s3"].replace("{basin}", basin)
            if "YOUR-" in uri.upper():
                print(f"  skipping {local.name}: fill in the real bucket in deploy_config.json")
                continue
            m = re.match(r"s3://([^/]+)/(.*)", uri)
            if not m:
                print(f"  bad s3 uri for {local.name}: {uri}")
                continue
            bucket, key = m.group(1), m.group(2)
            # A source that cannot be resolved is a warning, not a fatal error:
            # only the *ForecastWellMonths* files are needed to serve, and an
            # export that has not landed yet must not stop the box from starting.
            try:
                lm = re.search(r"\{latest:([^}]+)\}", key)
                if lm:
                    pat = lm.group(1)
                    ck = (bucket, pat)
                    if ck not in latest_cache:
                        latest_cache[ck] = _resolve_latest(s3, bucket, pat)
                        print(f"  latest export for {pat!r}: {latest_cache[ck]}")
                    key = key.replace(lm.group(0), latest_cache[ck])
                mm = re.search(r"\{match:([^}]+)\}", key)
                if mm:
                    key = _resolve_match(s3, bucket, key.replace(mm.group(0), mm.group(1)))
                    print(f"  matched object: {key}")
            except Exception as e:
                missing.append(local.name)
                print(f"  SKIPPING {local.name}: {e}")
                continue
            # Freshness: a launch should serve what is in S3 right now, so the
            # object's size and timestamp decide, not merely "a file is there".
            if local.exists() and not refresh:
                try:
                    h = s3.head_object(Bucket=bucket, Key=key)
                    s3_size = h["ContentLength"]
                    s3_time = h["LastModified"].timestamp()
                    st = local.stat()
                    if st.st_size == s3_size and st.st_mtime >= s3_time - 2:
                        print(f"  up to date: {local.name} ({s3_size:,} bytes)")
                        continue
                    why = (f"size {st.st_size:,} -> {s3_size:,}"
                           if st.st_size != s3_size else
                           "same size but the object was re-exported")
                    print(f"  STALE {local.name}: {why} — re-pulling")
                except Exception as e:
                    print(f"  could not check {local.name} against S3 ({e}); "
                          f"keeping the local copy")
                    continue
            local.parent.mkdir(parents=True, exist_ok=True)
            print(f"  downloading s3://{bucket}/{key} -> {local} …")
            # download to .part and rename, so a killed transfer leaves nothing
            # that a later run would mistake for a finished file and skip
            tmp = local.with_suffix(local.suffix + ".part")
            try:
                s3.download_file(bucket, key, str(tmp))
                tmp.replace(local)
                # stamp the local file with the object's own timestamp so the
                # next launch can tell "same object" from "new export"
                try:
                    ts = s3.head_object(Bucket=bucket, Key=key)["LastModified"].timestamp()
                    os.utime(local, (ts, ts))
                except Exception:
                    pass
                print(f"    done ({local.stat().st_size/1e9:.2f} GB)")
            except BaseException as e:
                try:
                    if tmp.exists(): tmp.unlink()
                except OSError:
                    pass
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                missing.append(local.name)
                print(f"    FAILED: {e}")

    if missing:
        print("\nsources not pulled: " + ", ".join(sorted(set(missing))))
        if [x for x in missing if "ForecastWellMonths" in x]:
            print("  ^ these ARE needed for the wedge / type curves — fix the "
                  "path in deploy_config.json and restart")
        else:
            print("  ^ none of these are needed to serve; they only matter for "
                  "rebuilding the dashboard HTML via build/build_all.py")

# ── rebuilding the dashboard HTML itself from fresh S3 data ─────────────────
# The HTML bakes in everything except production: stick geometry (from the
# WellboreLocations dots), well details, and the economics. Those sources are
# small (~450 MB), so on launch their S3 stamps are compared with what the
# current HTML was built from; when anything moved, the box downloads them,
# reruns the build chain, and swaps the HTML — no stale pilot data survives.
APP_STAMP = ROOT / "_app_build.json"
APP_BUILD_LOCALS = [
    "data/econ/all_{basin}.zip",       # combined one-file export (optional per basin)
    "data/econ/undrilled_{basin}.zip",
    "data/econ/drilled_{basin}.zip",
    "data/Undrilled_{basin}_WellboreLocations.csv",
    "data/WellDetails.tsv",
    "data/WellboreLocations.tsv",
]
BUILD_STEPS = ["build_welldata.py", "build_pads.py", "build_drilled.py", "assemble.py"]

def rebuild_app(s3, cfg, force=False):
    global DASH
    basins = cfg.get("basins", [""])
    by_local = {src["local"]: src["s3"] for src in cfg.get("sources", [])}
    optional = {src["local"] for src in cfg.get("sources", []) if src.get("optional")}
    plan, stamps = [], {}
    for tpl in APP_BUILD_LOCALS:
        uri = by_local.get(tpl)
        if uri is None:
            print(f"  app rebuild: no source configured for {tpl} — skipping the rebuild")
            return
        for basin in (basins if "{basin}" in tpl else [""]):
            local = ROOT / tpl.replace("{basin}", basin)
            try:
                bucket, key = _resolve_uri(s3, uri, basin)
                stamps[str(local.relative_to(ROOT))] = _stamp(s3, bucket, key)
                plan.append((local, bucket, key))
            except Exception as e:
                if tpl in optional:
                    print(f"  app rebuild: optional source {tpl} not on S3 for {basin!r} — building without it")
                    continue
                print(f"  app rebuild: cannot resolve {tpl} for {basin!r} ({e}) — skipping the rebuild")
                return
    if not force and APP_STAMP.exists():
        try:
            if json.loads(APP_STAMP.read_text()) == stamps and DASH is not None:
                print("  dashboard HTML is current (built from these exact S3 objects)")
                return
        except Exception:
            pass
    try:
        import pandas, geopandas  # noqa: F401
    except ImportError:
        print("NOTE: the S3 well/econ data is newer than this dashboard HTML, but "
              "pandas/geopandas are not installed so the box cannot rebuild it.\n"
              "      pip3 install --user -r build/requirements.txt   and restart.")
        return
    missing_static = [d for d in ("build", "app/template.html", "data/static")
                      if not (ROOT / d).exists()]
    if missing_static:
        print(f"NOTE: cannot rebuild the dashboard HTML — missing {missing_static} "
              f"(upload the build bundle next to serve.py)")
        return
    print("rebuilding the dashboard HTML from fresh S3 data…")
    for local, bucket, key in plan:
        local.parent.mkdir(parents=True, exist_ok=True)
        print(f"  pulling s3://{bucket}/{key} ({key.rsplit('/',1)[-1]})")
        tmp = local.with_suffix(local.suffix + ".part")
        s3.download_file(bucket, key, str(tmp))
        tmp.replace(local)
    import subprocess
    for step in BUILD_STEPS:
        print(f"  === {step} ===")
        r = subprocess.run([sys.executable, str(ROOT / "build" / step)], cwd=ROOT)
        if r.returncode:
            print(f"  {step} FAILED ({r.returncode}) — keeping the previous HTML")
            return
    APP_STAMP.write_text(json.dumps(stamps, indent=1))
    DASH = next(iter(sorted(ROOT.glob("*.html"))), None)
    # the raw inputs are build scratch — drop them so nothing stale lingers
    import shutil
    for pat in ("data/*.tsv", "data/*.csv"):
        for f in ROOT.glob(pat):
            try: f.unlink()
            except OSError: pass
    for d in (ROOT / "data/econ", ROOT / "build/out"):
        shutil.rmtree(d, ignore_errors=True)
    print(f"  dashboard HTML rebuilt: {DASH.name} "
          f"({DASH.stat().st_size/1e6:.1f} MB)" if DASH else "  rebuild produced no HTML?!")

# ── automatic caches built from the raw exports ─────────────────────────────
# The raw production exports are found automatically: in ./data, next to this
# script, or in any folder listed under "local_dirs" in deploy_config.json —
# so the files can stay wherever they already live, no copying or dragging.
# The first start builds compact caches (a few minutes, one time) and the
# dashboard then loads them instantly at /api/wedge and /api/tcurves.
# Delete the _cache_*.bin files to force a rebuild.
DATA = ROOT / "data"
def _local_dirs():
    dirs = [DATA, ROOT]
    try:
        for d in json.loads(CONFIG_S3.read_text()).get("local_dirs", []):
            pd = Path(d).expanduser()
            if pd.is_dir():
                dirs.append(pd)
    except Exception:
        pass
    return dirs
SRC_DIRS = _local_dirs()
def _find_src(name):
    for d in SRC_DIRS:
        if (d / name).exists():
            return d / name
    return DATA / name
WEDGE_SRC = _find_src("NGL_ForecastWellMonths.tsv")
WEDGE_CACHE = ROOT / "_cache_wedge.bin"
TC_CACHE = ROOT / "_cache_tcurves.bin"

def _resolve_uri(s3, uri, basin=""):
    """s3://bucket/key with {basin}, {latest:…} and {match:…} resolved."""
    uri = uri.replace("{basin}", basin)
    m = re.match(r"s3://([^/]+)/(.*)", uri)
    if not m:
        raise ValueError(f"not an s3 uri: {uri}")
    bucket, key = m.group(1), m.group(2)
    lm = re.search(r"\{latest:([^}]+)\}", key)
    if lm:
        key = key.replace(lm.group(0), _resolve_latest(s3, bucket, lm.group(1)))
    mm = re.search(r"\{match:([^}]+)\}", key)
    if mm:
        key = _resolve_match(s3, bucket, key.replace(mm.group(0), mm.group(1)))
    return bucket, key

class _S3Raw(io.RawIOBase):
    """boto3's StreamingBody as a real raw stream, so TextIOWrapper can wrap it
    and the builders can iterate an S3 object line by line without the file
    ever touching disk."""
    def __init__(self, body): self._b = body
    def readable(self): return True
    def readinto(self, buf):
        chunk = self._b.read(len(buf))
        if not chunk: return 0
        buf[:len(chunk)] = chunk
        return len(chunk)
    def close(self):
        try: self._b.close()
        except Exception: pass
        super().close()

def _s3_text(s3, bucket, key, bufsize=8 << 20):
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    return io.TextIOWrapper(io.BufferedReader(_S3Raw(body), buffer_size=bufsize),
                            encoding="utf-8", errors="replace")

def _stamp(s3, bucket, key):
    """Identity of the S3 object right now — size + last-modified. Recorded in
    the cache header so a restart can tell 'same export' from 'new export'."""
    h = s3.head_object(Bucket=bucket, Key=key)
    return f"{h['ContentLength']}:{h['LastModified'].isoformat()}"

def _purity_populated_s3(s3, bucket, key, sep, cols, size):
    """Same sampler as the local one, over ranged GETs."""
    if not cols: return False
    for frac in (0.02, 0.2, 0.4, 0.6, 0.8, 0.95):
        start = int(size * frac)
        rng = f"bytes={start}-{min(size - 1, start + (4 << 20))}"
        try:
            data = s3.get_object(Bucket=bucket, Key=key, Range=rng)["Body"].read()
        except Exception:
            return False
        for line in data.decode("utf-8", "replace").split("\n")[1:-1]:
            p = line.split(sep)
            for ix in cols:
                if ix < len(p):
                    v = p[ix].strip()
                    if v and v not in ("0", "0.0"):
                        return True
    return False

def _pack(hdr: dict, *bufs) -> bytes:
    h = json.dumps(hdr).encode()
    return gzip.compress(struct.pack("<I", len(h)) + h + b"".join(bufs), 6)

PURITY_COLS = [("ethane", "EthanePerDay"), ("propane", "PropanePerDay"),
               ("butane", "ButanePerDay"), ("isobutane", "IsobutanePerDay"),
               ("pentanes", "PentanesPerDay")]

def _purity_populated(path, sep, cols):
    """The purity columns exist in the schema but are usually empty — sample the
    file at several offsets rather than paying for five extra planes blindly."""
    if not cols:
        return False
    size = path.stat().st_size
    with open(path, "rb") as f:
        for frac in (0.02, 0.2, 0.4, 0.6, 0.8, 0.95):
            f.seek(int(size * frac))
            f.readline()                       # drop the partial line
            for _ in range(60000):
                line = f.readline()
                if not line:
                    break
                p = line.decode("utf-8", "replace").split(sep)
                for ix in cols:
                    if ix < len(p):
                        v = p[ix].strip()
                        if v and v not in ("0", "0.0"):
                            return True
    return False

def _cache_is_current(cache_path, stamps):
    """A cache built from exactly these source versions needs no rebuild."""
    if not cache_path.exists() or not stamps:
        return False
    # The header carries the full API / well-name list, so it can run to a few
    # hundred KB — read exactly the declared length, never a fixed cap, or the
    # JSON truncates and every restart silently rebuilds a current cache.
    try:
        with gzip.open(cache_path, "rb") as f:
            hl = struct.unpack("<I", f.read(4))[0]
            if hl > (64 << 20):
                return False
            hdr = json.loads(f.read(hl))
    except Exception:
        return False
    return hdr.get("src") == stamps

def build_wedge_cache(s3=None, uri=None):
    """Build _cache_wedge.bin. With `s3`+`uri` the export is streamed straight
    out of S3 and never written to disk; otherwise a local copy is used."""
    stamps, opener, label, size = None, None, None, 0
    if s3 is not None and uri:
        try:
            bucket, key = _resolve_uri(s3, uri)
            h = s3.head_object(Bucket=bucket, Key=key)
            size = h["ContentLength"]
            stamps = [f"{size}:{h['LastModified'].isoformat()}"]
            label = key.rsplit("/", 1)[-1]
            opener = lambda: _s3_text(s3, bucket, key)
        except Exception as e:
            print(f"  cannot reach the wedge source in S3 ({e})")
            return
    elif WEDGE_SRC.exists():
        size = WEDGE_SRC.stat().st_size
        stamps = [f"{size}:{int(WEDGE_SRC.stat().st_mtime)}"]
        label = WEDGE_SRC.name
        opener = lambda: open(WEDGE_SRC, "r", errors="replace")
    else:
        if not WEDGE_CACHE.exists():
            print("NOTE: NGL_ForecastWellMonths.tsv not found in "
                  + ", ".join(str(d) for d in SRC_DIRS)
                  + " and no wedge_source configured — the production wedge "
                    "will ask for a drag-and-drop.")
        return
    if _cache_is_current(WEDGE_CACHE, stamps):
        print(f"  wedge cache is current ({label})")
        return
    print(f"building wedge cache from {label} "
          f"({size/1e9:.1f} GB{' streamed from S3' if s3 is not None and uri else ''} "
          f"— a few minutes)…")
    START, M = 2010 * 12, 432
    with opener() as f:
        hdr = f.readline().rstrip("\n").split("\t")
    ai, di = hdr.index("API10"), hdr.index("Date")
    oi, ni = hdr.index("OilPerDay"), hdr.index("NGLPerDay")
    gi = hdr.index("DryGasPerDay")
    wi = hdr.index("GasPerDay") if "GasPerDay" in hdr else gi   # wet: gas opex basis
    pur_ix = [hdr.index(c) for _, c in PURITY_COLS if c in hdr]
    use_pur = (_purity_populated_s3(s3, bucket, key, "\t", pur_ix, size)
               if (s3 is not None and uri) else
               _purity_populated(WEDGE_SRC, "\t", pur_ix))
    # planes: oil, dry gas, total NGL, wet gas (+ the five purity products)
    streams = ["oil", "gas", "ngl", "wet"] + ([k for k, _ in PURITY_COLS] if use_pur else [])
    NS = len(streams)
    print(f"  streams: {', '.join(streams)}"
          + ("" if use_pur else "  (purity columns empty in this export)"))
    per = {}
    n = 0
    with opener() as f:
        f.readline()
        def num(p, ix):
            try:
                v = p[ix]
                return float(v) if v else 0.0
            except (ValueError, IndexError):
                return 0.0
        for line in f:
            p = line.split("\t")
            try:
                y = int(p[di][:4]); mo = int(p[di][5:7])
            except (ValueError, IndexError):
                continue
            t = y * 12 + mo - 1 - START
            if t < 0 or t >= M:
                continue
            oil = num(p, oi); gas = num(p, gi); wet = num(p, wi) or gas
            pv = [num(p, ix) for ix in pur_ix] if use_pur else []
            # NGL: sum of purity products when reported, else the aggregate stream
            ngl = sum(pv)
            if ngl <= 0:
                ngl = num(p, ni)
            if oil <= 0 and gas <= 0 and ngl <= 0:
                continue
            a = p[ai]
            arr = per.get(a)
            if arr is None:
                arr = per[a] = array.array("H", bytes(2 * NS * M))
            vals = [oil, gas, ngl, wet] + pv
            for sp, v in enumerate(vals):
                iv = int(v + .5)
                if iv > 0:
                    arr[sp * M + t] = 65534 if iv > 65534 else iv
            n += 1
            if n % 2_000_000 == 0:
                print(f"  {n:,} rows…")
    apis = list(per.keys())
    body = b"".join(per[a].tobytes() for a in apis)
    WEDGE_CACHE.write_bytes(_pack({"v": 3, "m": M, "start": "2010-01",
                                   "streams": streams, "src": stamps,
                                   "apis": apis}, body))
    print(f"  wedge cache ready: {len(apis):,} wells x {NS} streams, "
          f"{WEDGE_CACHE.stat().st_size/1e6:.1f} MB")

def build_tcurves_cache(s3=None, uris=None, basins=("",)):
    """Build _cache_tcurves.bin. With `s3`+`uris` each undrilled export is
    streamed from S3 and never written to disk."""
    srcs = []            # (label, size, stamp, opener)
    if s3 is not None and uris:
        for uri in uris:
            for basin in basins:
                try:
                    bucket, key = _resolve_uri(s3, uri, basin)
                    h = s3.head_object(Bucket=bucket, Key=key)
                    srcs.append((key.rsplit("/", 1)[-1], h["ContentLength"],
                                 f"{h['ContentLength']}:{h['LastModified'].isoformat()}",
                                 (lambda b=bucket, k=key: _s3_text(s3, b, k))))
                except Exception as e:
                    print(f"  SKIPPING type-curve source {uri} / {basin}: {e}")
    else:
        seen = set()
        for d in SRC_DIRS:
            if not d.exists():
                continue
            for p in sorted(d.glob("Undrilled_*ForecastWellMonths*.csv")):
                if "NGL" in p.name:      # the NGL variant is a different contract
                    continue
                if p.name in seen:
                    continue
                seen.add(p.name)
                srcs.append((p.name, p.stat().st_size,
                             f"{p.stat().st_size}:{int(p.stat().st_mtime)}",
                             (lambda q=p: open(q, "r", errors="replace"))))
    if not srcs:
        return
    stamps = [st for _, _, st, _ in srcs]
    if _cache_is_current(TC_CACHE, stamps):
        print(f"  type-curve cache is current ({len(srcs)} source(s))")
        return
    print(f"building type-curve cache from {len(srcs)} undrilled file(s)"
          f"{' streamed from S3' if s3 is not None and uris else ''}…")
    M = 360
    streams = ["oil", "gas", "ngl", "wet"]
    NS = len(streams)
    per = {}          # well -> [oil | dry gas | NGL | wet gas]
    for label, size, _stamp_, opener in srcs:
        print(f"  {label} ({size/1e9:.1f} GB)…")
        with opener() as f:
            hdr = f.readline().rstrip("\n").split(",")
            ii, wi = hdr.index("ip_day"), hdr.index("novi_wellname")
            oi = hdr.index("oil")
            weti = hdr.index("gas") if "gas" in hdr else -1   # wellhead wet gas
            ngi = hdr.index("calc_NGLPerDay") if "calc_NGLPerDay" in hdr else hdr.index("NGLPerDay")
            ngf = hdr.index("NGLPerDay") if "NGLPerDay" in hdr else ngi
            dgi = hdr.index("calc_DryGasPerDay") if "calc_DryGasPerDay" in hdr else hdr.index("DryGasPerDay")
            dgf = hdr.index("DryGasPerDay") if "DryGasPerDay" in hdr else dgi
            def num(p, ix):
                try:
                    v = p[ix]
                    return float(v) if v else 0.0
                except (ValueError, IndexError):
                    return 0.0
            for line in f:
                p = line.split(",")
                try:
                    ip = int(float(p[ii]))
                except (ValueError, IndexError):
                    continue
                mo = ip // 30 - 1
                if mo < 0 or mo >= M:
                    continue
                oil = num(p, oi)
                gas = num(p, dgi) or num(p, dgf)
                ngl = num(p, ngi) or num(p, ngf)
                wet = (num(p, weti) if weti >= 0 else 0.0) or gas
                w = p[wi]
                arr = per.get(w)
                if arr is None:
                    arr = per[w] = array.array("H", bytes(2 * NS * M))
                for sp, v in enumerate((oil, gas, ngl, wet)):
                    iv = int(v + .5)
                    if iv > 0:
                        arr[sp * M + mo] = 65534 if iv > 65534 else iv
    names = list(per.keys())
    body = b"".join(per[w].tobytes() for w in names)
    TC_CACHE.write_bytes(_pack({"v": 3, "m": M, "streams": streams,
                                "src": stamps, "names": names}, body))
    print(f"  type-curve cache ready: {len(names):,} wells x {NS} streams, "
          f"{TC_CACHE.stat().st_size/1e6:.1f} MB")

class H(SimpleHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/config":
            if CONFIG.exists():
                return self._json(200, json.loads(CONFIG.read_text()))
            return self._json(200, {})
        if path == "/api/projects":
            if PROJECTS.exists():
                return self._json(200, json.loads(PROJECTS.read_text()))
            return self._json(200, {})
        if path in ("/api/wedge", "/api/tcurves"):
            cache = WEDGE_CACHE if path == "/api/wedge" else TC_CACHE
            legacy = ROOT / ("wedge.bin" if path == "/api/wedge" else "tcurves.bin")
            src = cache if cache.exists() else (legacy if legacy.exists() else None)
            if src is None:
                return self._json(404, {"error": "no cache — put the raw export in this folder and restart"})
            body = src.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/favicon.ico":                  # no icon file; stop 404 noise
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path in ("/", "/index.html") and DASH:
            self.path = "/" + DASH.name
        return super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 50_000_000:
            return self._json(413, {"error": "too large"})
        raw = self.rfile.read(n)
        path = self.path.split("?")[0]
        if path in ("/api/config", "/api/projects"):
            try:
                json.loads(raw)                     # must be valid JSON
            except Exception:
                return self._json(400, {"error": "invalid JSON"})
            (CONFIG if path == "/api/config" else PROJECTS).write_bytes(raw)
            return self._json(200, {"ok": True})
        if path == "/api/export":
            try:
                j = json.loads(raw)
                name = SAFE.sub("_", Path(j["name"]).name) or "export.txt"
                data = (base64.b64decode(j["content_b64"])
                        if "content_b64" in j else j["content"].encode())
            except Exception as e:
                return self._json(400, {"error": str(e)})
            EXPORTS.mkdir(exist_ok=True)
            (EXPORTS / name).write_bytes(data)
            return self._json(200, {"ok": True, "url": f"/exports/{name}"})
        return self._json(404, {"error": "unknown endpoint"})

    def log_message(self, fmt, *a):                 # quieter logs
        # send_error() routes through here with (code, message) args, where
        # code is an HTTPStatus — format defensively or the 404 path itself
        # raises and the client gets a dropped connection instead of the error
        try:
            line = fmt % a
        except Exception:
            line = " ".join(str(x) for x in a)
        if "/api/" in line:
            sys.stderr.write("%s %s\n" % (self.address_string(), line))

if __name__ == "__main__":
    # nohup > build.log block-buffers stdout, which makes a 40-minute first
    # build look like a hung process — keep it line buffered
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    FLAGS = {"--refresh", "--download-raw", "--rebuild-app", "--no-app-rebuild"}
    args = [a for a in sys.argv[1:] if a not in FLAGS]
    port = int(args[0]) if args else 8080
    for _stale in ROOT.glob("data/**/*.part"):
        print(f"  removing incomplete download {_stale.name}")
        try: _stale.unlink()
        except OSError: pass

    cfg = json.loads(CONFIG_S3.read_text()) if CONFIG_S3.exists() else {}
    basins = cfg.get("basins", [""])
    wedge_uri = cfg.get("wedge_source")
    tc_uris = cfg.get("tcurve_sources") or []
    s3 = None
    if wedge_uri or tc_uris:
        try:
            s3 = _s3_client(cfg.get("region", "us-east-2"))
        except ImportError:
            print("boto3 not installed — cannot stream from S3 (pip install boto3)")

    # The raw exports are build input only: streamed straight out of S3 into the
    # caches, never stored. --download-raw keeps local copies (needed only when
    # you also want to rebuild the dashboard HTML on this box).
    if "--download-raw" in sys.argv:
        sync_s3(refresh="--refresh" in sys.argv)

    if s3 is None and (wedge_uri or tc_uris):
        pass
    elif s3 is None:
        try:
            s3 = _s3_client(cfg.get("region", "us-east-2")) if cfg.get("sources") else None
        except ImportError:
            s3 = None
    if s3 is not None and "--no-app-rebuild" not in sys.argv:
        try:
            rebuild_app(s3, cfg, force="--rebuild-app" in sys.argv)
        except Exception as e:
            print(f"  app rebuild failed ({e}) — keeping the previous HTML")
    if s3 is not None:
        build_wedge_cache(s3, wedge_uri)
        build_tcurves_cache(s3, tc_uris, basins)
    else:
        build_wedge_cache()
        build_tcurves_cache()
    for _p in (WEDGE_CACHE, TC_CACHE):
        if _p.exists():
            print(f"  {_p.name}: {_p.stat().st_size/1e6:.1f} MB")
    print(f"serving {DASH.name if DASH else ROOT} on http://0.0.0.0:{port}")
    HTTPServer(("0.0.0.0", port), H).serve_forever()
