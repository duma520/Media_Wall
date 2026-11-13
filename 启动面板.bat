@echo off
setlocal
REM ============================================================
REM  Media_Wall desktop control panel launcher (GUI).
REM
REM  v2.5.4: this .bat is now a thin wrapper that delegates to
REM  launch_panel.vbs (run by wscript with NO window), which starts
REM  Media_Wall.py with pythonw.exe in a HIDDEN window -> no black
REM  console flash at all.
REM
REM  Keep this file pure ASCII + CRLF (cmd decodes .bat with OEM
REM  codepage 936/GBK; UTF-8 Chinese bytes leak into the parser).
REM
REM  Usage: this-file.bat [--port 9000] [--no-autostart]
REM ============================================================

pushd "%~dp0" 2>nul

REM Delegate to the windowless VBS launcher (wscript hides its own console).
wscript.exe "%~dp0launch_panel.vbs" %*
endlocal
