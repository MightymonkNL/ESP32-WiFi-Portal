@echo off
rem Builds the firmware of all modules (admin\modules\*\firmware) and
rem "ESP32 Toolbox.exe" in this folder. Run again after changing a project or the app.
rem Requires Python 3 and PlatformIO.
cd /d "%~dp0"
python -m pip install --user --quiet pyinstaller pyserial pillow || goto :error
python admin\build_modules.py || goto :error
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "ESP32 Toolbox" ^
  --paths admin ^
  --exclude-module numpy --exclude-module PIL.ImageQt --exclude-module PIL.ImageTk ^
  --distpath "%~dp0." ^
  --workpath "%TEMP%\esp32-toolbox-build" ^
  --specpath "%TEMP%\esp32-toolbox-build" ^
  "admin\toolbox.pyw" || goto :error
echo.
python admin\make_package.py || goto :error
echo Done: "%~dp0ESP32 Toolbox.exe"
echo Package for another PC: dist\ESP32-Toolbox-^<date^>.zip
echo The exe uses the modules in admin\modules and esptool in admin\tools next to it.
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
