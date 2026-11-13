@echo off
setlocal
REM ============================================================
REM  Media_Wall watchdog launcher (windowless).
REM  v2.7.0: starts the persistent watchdog (port 8786) that lets
REM  any device on the LAN start / restart / stop the Media_Wall
REM  service from a browser (server.html or watchdog console).
REM
REM  Keep this file pure ASCII + CRLF.
REM ============================================================

pushd "%~dp0" 2>nul
wscript.exe "%~dp0watchdog.vbs" %*
endlocal
