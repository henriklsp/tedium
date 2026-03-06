@echo off
pyinstaller --onefile --windowed --name tedium tedium\__main__.py
echo.
echo Build complete. Output: dist\tedium.exe
