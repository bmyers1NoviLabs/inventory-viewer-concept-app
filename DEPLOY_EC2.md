# Deploying the Appalachia Inventory Dashboard to EC2

Three files make up the app:

```
Appalachia Inventory Dashboard.html   19 MB   the whole dashboard, data baked in
serve.py                                      stdlib server + S3 sync + cache builder
deploy_config.json                            where the raw exports live on S3
```

Everything else — the production wedge and the undrilled type curves — `serve.py`
pulls from S3 and caches on first start.

## 1. Instance

- **t3.medium** or larger. The model runs client-side; the server only builds
  caches once and serves static files. 2 vCPU / 4 GB is fine.
- **30 GB gp3 root volume.** The raw exports are ~8.5 GB and the default 8 GB
  AMI volume will fail partway through the download. Check before you start:

  ```bash
  df -h / | tail -1
  ```

- Amazon Linux 2023 or Ubuntu — anything with Python 3.9+.
- Security group: inbound TCP **8080** from your office/VPN CIDR (or 80 behind nginx).

## 2. IAM role (no keys on disk)

Attach an instance role with read access to the four buckets:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:ListBucket"],
    "Resource": [
      "arn:aws:s3:::novi-bulk-api-data",              "arn:aws:s3:::novi-bulk-api-data/*",
      "arn:aws:s3:::novi-master-undrilled-bulk-data", "arn:aws:s3:::novi-master-undrilled-bulk-data/*",
      "arn:aws:s3:::novi-master-inventory-economics", "arn:aws:s3:::novi-master-inventory-economics/*",
      "arn:aws:s3:::YOUR-APP-BUCKET",                 "arn:aws:s3:::YOUR-APP-BUCKET/*"
    ]
  }]
}
```

Verify it works before going further:

```bash
aws sts get-caller-identity
aws s3 ls s3://novi-bulk-api-data/bulk-api-data-raw/us-onshore/Appalachia/ | head
```

## 3. Install the app — from the git repo

The box is a checkout of `bmyers1NoviLabs/inventory-viewer-concept-app`, so a
deploy is `git push` from a laptop and `update.sh` on the box. Auth is a
**fine-grained PAT**: single repository, Contents = Read-only, nothing else —
it sits in plaintext in `~/.git-credentials`, so scope it to be worthless
anywhere but this repo.

```bash
sudo mkdir -p /opt/novi-dash && sudo chown $(whoami) /opt/novi-dash
sudo dnf install -y git python3-pip          # apt-get on Ubuntu
pip3 install --user boto3
pip3 install --user -r /opt/novi-dash/build/requirements.txt   # after first checkout

cd /opt/novi-dash
git init -b main
git remote add origin https://github.com/bmyers1NoviLabs/inventory-viewer-concept-app.git
git config credential.helper store
GIT_ASKPASS=/bin/true git fetch origin   # first fetch: paste user + PAT when prompted
git reset --hard origin/main
```

Runtime files (`_cache_*.bin`, `config.json`, `projects.json`, `exports/`,
`_app_build.json`, `build.log`) are all gitignored, so a reset never touches
them — an existing S3-based install converts in place with the same commands.

To roll out a change later, from the repo root on the box:

```bash
./update.sh          # fetch + reset to origin/main, force an HTML freshness
                     # re-check, restart the service
```

## 4. First start

```bash
cd /opt/novi-dash
python3 serve.py 8080
```

Every launch checks each S3 source's identity (size + LastModified) against
what it last built from, then does only what changed:

1. **Dashboard HTML rebuild** — when the econ zips, `Undrilled_*_WellboreLocations`
   dots, or `WellDetails.tsv` moved, it pulls them (~450 MB), reruns the
   `build/` chain (needs pandas/geopandas), swaps the HTML, and deletes the raw
   inputs. Any step failing keeps the previous HTML serving.
2. **Production caches** — `NGL_ForecastWellMonths.tsv` (4.6 GB) and the
   undrilled ForecastWellMonths CSVs are **streamed straight out of S3** into
   `_cache_wedge.bin` / `_cache_tcurves.bin` (per-well uint16 planes:
   oil | dry gas | NGL | wet gas, plus the five purity products the moment an
   export populates them). The raw files never touch disk.
3. Serve. Steady-state disk is ~70 MB.

First cold start ≈ 20–30 minutes; a start where nothing moved in S3 is
seconds, printing `dashboard HTML is current` / `wedge cache is current` /
`type-curve cache is current`. Flags: `--rebuild-app` (force the HTML rebuild),
`--no-app-rebuild` (skip it), `--download-raw` (keep local copies of the raw
exports, only useful for debugging the build).

## 5. Keep it running (systemd)

`/etc/systemd/system/novi-dash.service`:

```ini
[Unit]
Description=Novi Appalachia Inventory Dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=ssm-user                       # whoever owns /opt/novi-dash (ec2-user on some AMIs)
Environment=PYTHONPATH=/home/ssm-user/.local/lib/python3.9/site-packages
WorkingDirectory=/opt/novi-dash
ExecStart=/usr/bin/python3 /opt/novi-dash/serve.py 8080
Restart=on-failure
RestartSec=5
TimeoutStartSec=3600

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now novi-dash
journalctl -u novi-dash -f          # watch the first build
```

`TimeoutStartSec=3600` matters — without it systemd kills the service mid-download
on a cold start.

## 6. What users get

`http://<instance>:8080` — or, with no port open at all, tunnel over Session
Manager from a laptop and browse to http://localhost:8080:

```
aws ssm start-session --target <instance-id> \
  --document-name AWS-StartPortForwardingSession \
  --parameters "portNumber=8080,localPortNumber=8080"
```

The dashboard, wedge and type curves are already loaded.
Every session opens on the full unfiltered data set and asks for a project.
Saved projects and filter settings round-trip to the server (`projects.json`,
`config.json`) so they follow the dashboard rather than one person's browser.
Quick-Eval exports land in `./exports/`.

## 7. Refreshing after a new export lands

```bash
sudo systemctl restart novi-dash
```

That's it — the launch stamp-check notices anything that moved in S3 and
rebuilds exactly that (HTML, wedge cache, type-curve cache). To force a full
redo regardless: `rm -f _cache_*.bin _app_build.json` first.

## Notes

- Code changes ship through git (`./update.sh`); data changes ship themselves —
  new S3 exports are picked up at the next restart. Redrawn static shapefiles
  (`data/static/`) are code as far as the box is concerned: commit + push + update.
- The tcurves builder skips `Undrilled_*_NGL_ForecastWellMonths.csv` — different
  contract from the file it wants.
- To front it with TLS, put nginx or an ALB in front of 8080; the app itself has
  no auth.
