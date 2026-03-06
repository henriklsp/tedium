@echo off
pyinstaller --onefile --windowed --name tedium main.py
echo.
echo Build complete. Output: dist\tedium.exe
