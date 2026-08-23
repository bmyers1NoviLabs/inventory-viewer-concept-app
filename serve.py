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
import array, base64, glob, gzip, json, re, struct, sys
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
    for src in cfg.get("sources", []):
        per_basin = "{basin}" in src["local"] or "{basin}" in src["s3"]
        for basin in (basins if per_basin else [""]):
            local = ROOT / src["local"].replace("{basin}", basin)
            if local.exists() and not refresh:
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
            local.parent.mkdir(parents=True, exist_ok=True)
            print(f"  downloading s3://{bucket}/{key} -> {local} …")
            try:
                s3.download_file(bucket, key, str(local))
                print(f"    done ({local.stat().st_size/1e9:.2f} GB)")
            except Exception as e:
                print(f"    FAILED: {e}")

# ── automatic caches built from the raw exports ─────────────────────────────
# Put NGL_ForecastWellMonths.tsv (and the Undrilled_*_ForecastWellMonths.csv
# files) in this folder; the first start builds compact caches (a few minutes,
# one time) and the dashboard then loads them instantly at /api/wedge and
# /api/tcurves. Delete the _cache_*.bin files to force a rebuild.
DATA = ROOT / "data"
def _find_src(name):
    for p in (DATA / name, ROOT / name):
        if p.exists():
            return p
    return DATA / name
WEDGE_SRC = _find_src("NGL_ForecastWellMonths.tsv")
WEDGE_CACHE = ROOT / "_cache_wedge.bin"
TC_CACHE = ROOT / "_cache_tcurves.bin"

def _pack(hdr: dict, *bufs) -> bytes:
    h = json.dumps(hdr).encode()
    return gzip.compress(struct.pack("<I", len(h)) + h + b"".join(bufs), 6)

def build_wedge_cache():
    if not WEDGE_SRC.exists():
        return
    if WEDGE_CACHE.exists() and WEDGE_CACHE.stat().st_mtime >= WEDGE_SRC.stat().st_mtime:
        return
    print(f"building wedge cache from {WEDGE_SRC.name} "
          f"({WEDGE_SRC.stat().st_size/1e9:.1f} GB — one time, a few minutes)…")
    START, M = 2010 * 12, 432
    per = {}
    n = 0
    with open(WEDGE_SRC, "r", errors="replace") as f:
        hdr = f.readline().rstrip("\n").split("\t")
        ai, di, gi = hdr.index("API10"), hdr.index("Date"), hdr.index("GasPerDay")
        for line in f:
            p = line.split("\t")
            try:
                y = int(p[di][:4]); mo = int(p[di][5:7]); g = float(p[gi] or 0)
            except (ValueError, IndexError):
                continue
            t = y * 12 + mo - 1 - START
            if t < 0 or t >= M or g <= 0:
                continue
            a = p[ai]
            arr = per.get(a)
            if arr is None:
                arr = per[a] = array.array("H", bytes(2 * M))
            v = int(g + .5)
            arr[t] = 65534 if v > 65534 else v
            n += 1
            if n % 2_000_000 == 0:
                print(f"  {n:,} rows…")
    apis = list(per.keys())
    body = b"".join(per[a].tobytes() for a in apis)
    WEDGE_CACHE.write_bytes(_pack({"m": M, "start": "2010-01", "apis": apis}, body))
    print(f"  wedge cache ready: {len(apis):,} wells, {WEDGE_CACHE.stat().st_size/1e6:.1f} MB")

def build_tcurves_cache():
    srcs = sorted(p for pat in (ROOT, DATA) if pat.exists()
                  for p in pat.glob("Undrilled_*ForecastWellMonths*.csv")
                  if "NGL" not in p.name)      # the NGL variant is a different contract
    if not srcs:
        return
    newest = max(s.stat().st_mtime for s in srcs)
    if TC_CACHE.exists() and TC_CACHE.stat().st_mtime >= newest:
        return
    print(f"building type-curve cache from {len(srcs)} undrilled file(s) — one time…")
    M = 360
    per, oilc, gasc = {}, {}, {}
    for src in srcs:
        print(f"  {src.name} ({src.stat().st_size/1e9:.1f} GB)…")
        with open(src, "r", errors="replace") as f:
            hdr = f.readline().rstrip("\n").split(",")
            ii, ni = hdr.index("ip_day"), hdr.index("novi_wellname")
            oi, gi = hdr.index("oil"), hdr.index("gas")
            for line in f:
                p = line.split(",")
                try:
                    ip = int(float(p[ii])); g = float(p[gi] or 0); o = float(p[oi] or 0)
                except (ValueError, IndexError):
                    continue
                mo = ip // 30 - 1
                if mo < 0:
                    continue
                w = p[ni]
                if mo < M:
                    arr = per.get(w)
                    if arr is None:
                        arr = per[w] = array.array("H", bytes(2 * M))
                    v = int(g + .5)
                    arr[mo] = 65534 if v > 65534 else v
                gasc[w] = gasc.get(w, 0.0) + g * 30
                oilc[w] = oilc.get(w, 0.0) + o * 30
    names = list(per.keys())
    oy = array.array("H", (min(65534, int((oilc[w] / (gasc[w] / 1000) if gasc[w] > 0 else 0) * 10 + .5)) for w in names))
    body = b"".join(per[w].tobytes() for w in names)
    TC_CACHE.write_bytes(_pack({"m": M, "oyscale": 0.1, "names": names}, oy.tobytes(), body))
    print(f"  type-curve cache ready: {len(names):,} wells, {TC_CACHE.stat().st_size/1e6:.1f} MB")

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
        if "/api/" in (a[0] if a else ""):
            sys.stderr.write("%s %s\n" % (self.address_string(), a[0]))

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--refresh"]
    port = int(args[0]) if args else 8080
    sync_s3(refresh="--refresh" in sys.argv)
    build_wedge_cache()
    build_tcurves_cache()
    print(f"serving {DASH.name if DASH else ROOT} on http://0.0.0.0:{port}")
    HTTPServer(("0.0.0.0", port), H).serve_forever()
