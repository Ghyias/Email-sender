@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtualenv...
  py -3.11 -m venv .venv 2>nul || python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)

echo Checking Ollama on localhost:11434 ...
curl -s http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
  echo WARNING: Ollama not reachable. Draft/CV features need Ollama running.
)

echo Starting MailPilot on http://127.0.0.1:8765 ...
echo Keep this window open. Opening the browser in a few seconds...
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:8765/"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
endlocal
