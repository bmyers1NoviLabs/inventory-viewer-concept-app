# Inventory Viewer — concept app

A self-contained inventory dashboard for Appalachia (Utica + Marcellus): map of
undrilled wellbores, Novi-style filters with saved Projects, screening charts
(density / cost curve / rainclouds / tranches / quartiles / cross plots),
drilled-vs-remaining trends, and a Quick-Eval tab with the PDP blowdown model,
drill-out scheduler, and PDP + PUD production wedge. Every chart exports to a
native Excel chart; the map exports to PNG.

## Run it

```bash
pip install boto3            # only needed for the S3 sync
python serve.py 8080         # first start: syncs S3 sources + builds caches
```

Open http://localhost:8080. On Windows, `Open Dashboard.bat` does the same in
one double-click. `--refresh` re-downloads sources after a new export lands.

`serve.py` is stdlib-only apart from boto3, serves the prebuilt
`Appalachia Inventory Dashboard.html`, exposes `/api/wedge`, `/api/tcurves`,
`/api/config`, `/api/projects`, `/api/export`, and builds its production caches
from the raw exports named in `deploy_config.json`.

## Data flow

```
deploy_config.json           which basins + where each source lives on S3
        │  ({basin} expansion, {latest:...} newest prefix, {match:...} newest object)
serve.py --refresh           downloads to data/
build/build_all.py           welldata -> pads -> drilled -> assemble
        │                    (undrilled econ + locations + phase windows + PUD/RES
        │                     polygons + pad polygons + drilled econ + WellDetails)
Appalachia Inventory Dashboard.html      the single-file app (committed)
serve.py                     wedge + type-curve caches from the raw production files
```

The dashboard also runs with zero backend: double-click the HTML and drop
`NGL_ForecastWellMonths.tsv` / the undrilled ForecastWellMonths CSVs on the map
once — parsed in-browser and cached.

## Layout

```
app/template.html        the application (placeholders baked by build/assemble.py)
app/assets/              basemap, logo, Poppins subset
build/                   data pipeline (see build_all.py; needs geopandas)
data/static/             committed inputs: phase windows, PUD/RES + pad shapefile zips
serve.py                 server + S3 sync + production caches
deploy_config.json       S3 source patterns
DEPLOY_EC2.md            EC2 walkthrough (IAM role, systemd, sizing)
```
