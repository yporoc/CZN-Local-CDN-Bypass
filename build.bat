@echo off
rem Build script: PyInstaller onefile exe + default empty czn_cdn.ini
rem Requirements: Python 3.10+ with pillow and cryptography installed
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo [x] Python launcher "py" not found. Install Python 3.10+ first.
    pause & exit /b 1
)

py -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [..] Installing pyinstaller ...
    py -m pip install --quiet pyinstaller
    if errorlevel 1 (
        echo [x] Failed to install pyinstaller.
        pause & exit /b 1
    )
)

py -c "import PIL, cryptography" >nul 2>nul
if errorlevel 1 (
    echo [x] Missing deps. Run: pip install -r requirements.txt
    pause & exit /b 1
)

echo [..] Building onefile exe ...
py -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --name "CZN-Local-CDN-Responder" ^
    --add-data "assets;assets" ^
    czn_gui.py
if errorlevel 1 (
    echo [x] Build failed.
    pause & exit /b 1
)

rem Default config: empty on purpose; the GUI fills "gameres =" when a
rem folder is picked. Engine treats an empty value as "not configured".
> "dist\czn_cdn.ini" echo [game]
>> "dist\czn_cdn.ini" echo gameres =

echo.
echo [OK] Output in dist\ :
dir /b "dist"
echo.
echo Files: CZN-Local-CDN-Responder.exe + czn_cdn.ini (keep them together)
pause
