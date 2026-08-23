"""Full rebuild: S3 sources -> dashboard HTML.

    python serve.py --refresh     # (or just once) pull sources per deploy_config.json
    python build/build_all.py     # welldata -> pads -> drilled -> assemble

Then (re)start serve.py — it rebuilds the wedge/type-curve caches automatically
because the raw production files are newer than the caches.

Needs: pandas, numpy, geopandas, shapely  (pip install -r build/requirements.txt)
"""
import subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for step in ("build_welldata.py", "build_pads.py", "build_drilled.py", "assemble.py"):
    print(f"\n=== {step} ===")
    r = subprocess.run([sys.executable, str(HERE / step)], cwd=HERE.parent)
    if r.returncode:
        sys.exit(f"{step} failed ({r.returncode})")
print("\nBuild complete. Restart serve.py to refresh the wedge/type-curve caches.")
