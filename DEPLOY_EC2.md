# Deploying the Appalachia Inventory Dashboard to EC2

The whole deployment is four files: `Appalachia Inventory Dashboard.html`, `serve.py`,
`deploy_config.json`, and (optionally) pre-built `_cache_*.bin` files. serve.py pulls the
raw production exports from S3 on startup, builds its caches once, and serves everything —
users just open the URL. No drag-and-drop anywhere.

## 1. Instance

- t3.medium or larger (the one-time cache build reads the 4 GB TSV; 2 vCPU / 4 GB RAM is fine).
- 30 GB gp3 disk (raw exports ~8 GB + caches + headroom).
- Amazon Linux 2023 or Ubuntu — anything with Python 3.9+.
- Security group: allow inbound TCP 8080 (or 80 if you front it with nginx) from your office/VPN CIDR.

## 2. IAM role (no keys on disk)

Attach an instance role with read access to the buckets in `deploy_config.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:ListBucket"],
    "Resource": [
      "arn:aws:s3:::novi-undrilled-redshift-working-bucket",
      "arn:aws:s3:::novi-undrilled-redshift-working-bucket/*",
      "arn:aws:s3:::YOUR-DRILLED-BUCKET",
      "arn:aws:s3:::YOUR-DRILLED-BUCKET/*"
    ]
  }]
}
```

## 3. Configure the S3 sources

Edit `deploy_config.json`. Patterns are f-string style:

- `{basin}` expands for every entry in `"basins"` — so
  `Undrilled_{basin}_ForecastWellMonths.csv` finds both the Utica and Marcellus files.
- `{latest:{basin}_*}` resolves to the **newest** timestamped export prefix in the bucket
  (your CLI uploads to `<Basin>_<YYYY-MM-DD_HHMMSS>/`, which sorts chronologically), so the
  dashboard always deploys against the most recent run — no hardcoded folder names.

The undrilled entry is already pointed at `novi-undrilled-redshift-working-bucket` per the
CLI's upload convention. **Fill in the drilled NGL_ForecastWellMonths.tsv location** — the
placeholder bucket is intentionally invalid and is skipped until you set it.

## 4. Install and run

```bash
sudo yum install -y python3 python3-pip        # (apt on Ubuntu)
pip3 install boto3
mkdir -p /opt/novi-dash && cd /opt/novi-dash
#   copy in: Appalachia Inventory Dashboard.html, serve.py, deploy_config.json
python3 serve.py 8080
```

First start: downloads whatever is missing from S3, then builds the two caches
(a few minutes for the 4 GB TSV — progress prints). Every later start is instant.
`python3 serve.py 8080 --refresh` forces a re-download (e.g. after a new export lands),
and deleting the `_cache_*.bin` files forces a cache rebuild.

## 5. Keep it running (systemd)

`/etc/systemd/system/novi-dash.service`:

```ini
[Unit]
Description=Novi Appalachia Inventory Dashboard
After=network.target

[Service]
WorkingDirectory=/opt/novi-dash
ExecStart=/usr/bin/python3 serve.py 8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now novi-dash
```

## 6. What users get

`http://<instance>:8080` — the dashboard with the wedge and type curves loaded
automatically, filter settings and saved Projects stored server-side (`config.json`,
`projects.json`), and Quick-Eval exports landing in `./exports/`. To refresh data after a
new CLI export: `sudo systemctl stop novi-dash && python3 serve.py 8080 --refresh` once
(or just delete the raw + cache files and restart the service).

## Notes

- The `{latest:...}` resolver takes the lexicographically last matching prefix, which for
  `<Basin>_<timestamp>` naming is the newest export.
- The tcurves builder ignores `Undrilled_*_NGL_ForecastWellMonths.csv` (different contract).
- Everything also still works with the files placed in the folder manually — S3 is only
  consulted for files that aren't already present.
