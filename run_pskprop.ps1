#change to the location where you have your pskprop app.py installed
cd "C:\Users\kleht\Documents\GitHub\pskprop"

# create venv if missing
if (-not (Test-Path ".\.venv")) {
    py -m venv .venv
}
# was py -3.11 -m venv .venv


# activate
.\.venv\Scripts\Activate.ps1

# install deps (safe to run even if already installed)
pip install -r requirements.txt

# launch the app in background
Start-Process powershell -ArgumentList "uvicorn app:app --host 0.0.0.0 --port 8080"

# give Uvicorn ~2 seconds to start
Start-Sleep -Seconds 2

# open browser
Start-Process "http://localhost:8080"
