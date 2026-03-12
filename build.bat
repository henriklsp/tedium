@echo off
pyinstaller --onefile --windowed --name tedium --icon assets\tedium.ico --add-data assets\tedium.ico;assets tedium\__main__.py
echo.
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\tedium.iss
echo.
echo Build complete. Installer: installer\Output\tedium-setup.exe
