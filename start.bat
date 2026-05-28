@echo off
cd /d "%~dp0backend"
REM Add winget FFmpeg to PATH for this session (if installed but not on system PATH)
for /d %%D in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*") do (
  for /d %%B in ("%%D\ffmpeg-*") do set "PATH=%%B\bin;%PATH%"
)
echo VidToDoc - starting server at http://127.0.0.1:8000
echo Keep this window open while using the app.
echo.
set "PY312=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%PY312%" (
  "%PY312%" main.py
) else (
  python main.py
)
if errorlevel 1 (
  echo.
  echo Failed to start. Try: %PY312% -m pip install -r requirements.txt
  pause
)
