@echo off
setlocal enabledelayedexpansion
REM Switch the console to UTF-8 so server.py's Chinese banner prints
REM correctly instead of mojibake. 65001 works on Windows 10+.
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
REM ============================================================
REM  Media_Wall multi-category poster-wall launcher
REM
REM  NOTE: this file MUST stay pure ASCII.
REM  Windows cmd reads .bat with the OEM code page (936/GBK here);
REM  UTF-8 Chinese bytes get mangled and then parsed as commands,
REM  which produces errors like:
REM    'xxx' is not recognized as an internal or external command
REM  So: no Chinese characters in this file. Put Chinese text only
REM  in the .py / .html / .md files, which are read as UTF-8.
REM
REM  Usage:  this-file.bat [port]        (default port 8787)
REM
REM  Media root (the NAS Download share with 16 category dirs)
REM  is NOT derived from this script's location. It comes from
REM  config.json -> media_root
REM  (default: \\NAS\Share\Download).
REM  Category list is auto-discovered from that root; an optional
REM  "categories" whitelist in config.json pins order/subset.
REM
REM  Port 8787 (v2.5.2): same as AV_Wall; the two services are
REM  not expected to run at the same time on this machine.
REM ============================================================

pushd "%~dp0" 2>nul

REM ---- optional port argument ----
set "PORT=%~1"
if not defined PORT set "PORT=8787"

REM ---- locate python interpreter (priority order) ----
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py" )
if not defined PY (
  if exist "D:\Program Files\Python310\python.exe" set "PY=D:\Program Files\Python310\python.exe"
)
if not defined PY (
  echo [mediawall] Could not find Python. Please install Python 3.10 or add it to PATH.
  pause
  exit /b 1
)

echo [mediawall] Starting multi-category poster-wall server. Open the address below (or scan the QR) in a browser.
REM The service stops ONLY on Ctrl+C in this window (or POST /api/shutdown
REM from localhost). There is no "stop when the page is closed" watchdog.
echo [mediawall] Tip: press Ctrl+C in this window to stop the service.
echo.

REM ---- kill any old process holding the port, so a restart really takes effect ----
REM findstr /C:":8787 " is an EXACT substring match on the port column.
REM Do NOT use a regex like ":8787 .*LISTENING" here: that is a
REM substring match too and it also matches ports like 7, 9, 80, 135,
REM 445, 3389 ... which would make this script kill unrelated system
REM processes (PID 4 = System, 135 = RPC, 445 = SMB ...).
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":%PORT% " ^| findstr /C:"LISTENING"') do (
  if not "%%a"=="" (
    echo [mediawall] Killing old process on port %PORT%, PID=%%a ...
    taskkill /F /PID %%a >nul 2>nul
  )
)
REM ping -n 2 is used instead of "timeout /t 1": ping works the same
REM in cmd.exe and in Git Bash, while "timeout" may be shadowed by the
REM GNU coreutils timeout that ships with Git Bash.
ping -n 2 127.0.0.1 >nul

REM ---- verify the port is really free before starting ----
REM If the old process could not be killed (for example it runs under
REM another account), server.py would fail to bind. On Windows a second
REM process can hijack an already-listening port, which used to make two
REM servers share the port and answer requests at random. server.py now
REM refuses to start in that case and prints a clear message, but it is
REM friendlier to say it here.
set "PORT_BUSY=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":%PORT% " ^| findstr /C:"LISTENING"') do (
  if not "%%a"=="" set "PORT_BUSY=1"
)
if "!PORT_BUSY!"=="1" (
  echo [mediawall] Port %PORT% is still in use. The old process could not be stopped.
  echo [mediawall] Close the window that runs the old server ^(or press Ctrl+C there^), then try again.
  pause
  exit /b 1
)

"%PY%" "%~dp0server.py" --port %PORT%
endlocal
