@echo off
rem ── Appalachia Inventory Dashboard launcher ────────────────────────────
rem Starts serve.py in this folder and opens the dashboard in your browser.
rem First run builds caches from NGL_ForecastWellMonths.tsv and the
rem Undrilled_*_ForecastWellMonths.csv files (a few minutes, one time).
cd /d "%~dp0"
echo Starting the dashboard server...
echo (first run caches the production files - watch this window for progress)
start /b "" cmd /c "py -3 serve.py 8080 2>nul || python serve.py 8080"
:wait
powershell -noprofile -command "try{(New-Object Net.Sockets.TcpClient('127.0.0.1',8080)).Close();exit 0}catch{exit 1}" >nul 2>nul
if errorlevel 1 (
  timeout /t 2 /nobreak >nul
  goto wait
)
start "" http://localhost:8080
echo Dashboard is running at http://localhost:8080 - keep this window open.
pause >nul
