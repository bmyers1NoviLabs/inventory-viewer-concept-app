"""Bake build/out data into app/template.html -> the deployable single file."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP, OUT = ROOT / "app", ROOT / "build/out"

t = (APP / "template.html").read_text()
bm = (OUT / "basemap.json").read_text().replace("</", "<\\/")
out = (t.replace("__WELLDATA__", (OUT / "welldata.gz.b64").read_text())
        .replace("__BASEMAP__", bm)
        .replace("__LOGO__", (APP / "assets/mark_uri.txt").read_text().strip())
        .replace("__POPPINS__", (APP / "assets/poppins.css").read_text()))
dest = ROOT / "Appalachia Inventory Dashboard.html"
dest.write_text(out)
print(f"{dest.name}: {os.path.getsize(dest)/1e6:.1f} MB")
