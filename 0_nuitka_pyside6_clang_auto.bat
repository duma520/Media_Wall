@echo off
chcp 65001 >nul
setlocal
rem ============================================================
rem  Media_Wall - Nuitka build script (the ONLY build entry)
rem  Source of truth: Media_Wall.py  (do NOT use copies under version\)
rem
rem  PySide6 app => plugin pyside6 is required.
rem  Output: standalone dir build_output\Media_Wall.dist\  (ship the whole dir)
rem
rem  Packaging rules (keep in sync when the app changes):
rem    - index.html MUST ship inside the dist (HTTP server serves it)
rem    - server.html MUST ship too (v2.7.0 service control page)
rem    - data\ / config.json / gui_settings.json are NOT packaged:
rem      they are created next to the exe at runtime
rem    - icon.ico is optional; skipped when missing (app checks too)
rem    - Nuitka does NOT emit server.py / scanner.py / watchdog.py as
rem      standalone scripts; the exe re-launches itself with hidden modes
rem      --serve / --scan / --watchdog (see _dispatch_hidden_mode in Media_Wall.py)
rem    - watchdog launchers (watchdog.vbs, the start-watchdog .bat, watchdog.py)
rem      are COPIED into the dist after a successful build (step [3/3b]),
rem      so the shipped folder has a double-click entry out of the box
rem
rem  Compiler auto-detect: clang -> mingw64 gcc -> default MSVC
rem  Mostly ASCII + CRLF; the few Chinese launcher filenames stay UTF-8:
rem  chcp 65001 is set at the top and cmd copies them correctly (verified).
rem ============================================================
cd /d "%~dp0"
cls
echo [1/3] Cleaning old output
rmdir /s /q build_output 2>nul
rmdir /s /q app.build 2>nul
rmdir /s /q app.dist 2>nul
rmdir /s /q Media_Wall.build 2>nul
rmdir /s /q Media_Wall.dist 2>nul

echo [2/3] Detecting compiler and icon
set CC_OPT=
where clang >nul 2>nul && set CC_OPT=--clang
if defined CC_OPT goto :cc_done
where gcc >nul 2>nul && set CC_OPT=--mingw64
:cc_done
if defined CC_OPT goto :cc_show
echo    [!] clang/gcc not found, falling back to MSVC (needs VS Build Tools)
:cc_show
echo    compiler flags: %CC_OPT%

set ICON_OPT=
if exist "icon.ico" set ICON_OPT=--windows-icon-from-ico=icon.ico
if defined ICON_OPT goto :icon_done
echo    [i] icon.ico not found, skipping icon
:icon_done

echo [3/3] Building (first run takes 5-15 min, keep this window open)
nuitka --standalone ^
    --enable-plugin=pyside6 ^
    --windows-console-mode=disable ^
    --assume-yes-for-downloads ^
    --jobs=4 ^
    --remove-output ^
    --output-dir=build_output ^
    --include-data-files=index.html=index.html ^
    --include-data-files=server.html=server.html ^
    %CC_OPT% ^
    %ICON_OPT% ^
    Media_Wall.py

if errorlevel 1 goto :failed

rem ---- [3/3b] Ship watchdog launchers into the dist (v2.10.1) ----
rem  watchdog.vbs resolves the program dir from its OWN location and
rem  detects a compiled build by finding Media_Wall.exe next to it.
rem  So all three launchers MUST sit in the same folder as the exe,
rem  otherwise the shipped dist has no double-click way to start the
rem  watchdog. Nuitka is NOT used for these (Chinese filenames in a
rem  ^-continued command line are fragile); a plain copy is verified OK.
set "WD_DIST=build_output\Media_Wall.dist"
if not exist "%WD_DIST%\Media_Wall.exe" goto :copy_failed
copy /Y "%~dp0watchdog.vbs" "%WD_DIST%\" >nul 2>&1
if errorlevel 1 goto :copy_failed
copy /Y "%~dp0watchdog.py" "%WD_DIST%\" >nul 2>&1
if errorlevel 1 goto :copy_failed
copy /Y "%~dp0启动看门狗.bat" "%WD_DIST%\" >nul 2>&1
if errorlevel 1 goto :copy_failed
if not exist "%WD_DIST%\watchdog.vbs" goto :copy_failed
if not exist "%WD_DIST%\启动看门狗.bat" goto :copy_failed
echo      launchers : watchdog.vbs / start-watchdog .bat / watchdog.py copied
echo.
echo [OK] Build finished
echo      output dir : build_output\Media_Wall.dist\
echo      executable : build_output\Media_Wall.dist\Media_Wall.exe
echo      distribute : copy the whole Media_Wall.dist folder
pause
exit /b 0

:copy_failed
echo.
echo [X] Build OK but watchdog launchers were NOT copied - check paths above
pause
exit /b 1

:failed
echo.
echo [X] Build FAILED - send the error lines above back
pause
exit /b 1
