@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv" (
    python -m venv .venv
    call .venv\Scripts\activate.bat
    if exist "requirements.txt" (
        pip install -r requirements.txt
    ) else (
        echo [warn] no requirements.txt - installing common OCR snip deps
        pip install pytesseract pillow keyboard mss pyperclip
    )
    pip install pyinstaller
) else (
    call .venv\Scripts\activate.bat
)

set ICON_ARGS=
if exist "app.ico" (
    set ICON_ARGS=--icon=app.ico --add-data "app.ico;."
) else (
    echo [warn] app.ico not found - building without an icon
)

set MANIFEST_ARGS=
if exist "OCRSnip.manifest" (
    set MANIFEST_ARGS=--manifest "OCRSnip.manifest"
) else (
    echo [warn] OCRSnip.manifest not found - exe will be system-DPI-aware and
    echo        the snip overlay may be short/offset on scaled external monitors
)

python -m PyInstaller --onefile --noconsole --name OCRSnip %ICON_ARGS% %MANIFEST_ARGS% ocr_snip.py

echo.
echo Build complete: dist\OCRSnip.exe
echo.
echo NOTE: portable Tesseract is NOT bundled by this build - the exe
echo expects to find it at whatever path ocr_snip.py points to.
