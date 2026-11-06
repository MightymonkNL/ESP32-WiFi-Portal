@echo off
rem Builds "Portal Admin.exe" in this folder from admin\portal_admin.pyw.
rem Run again after changing the admin app. Requires Python 3.
cd /d "%~dp0"
python -m pip install --user --quiet pyinstaller pyserial || goto :error
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "Portal Admin" ^
  --distpath "%~dp0." ^
  --workpath "%TEMP%\portal-admin-build" ^
  --specpath "%TEMP%\portal-admin-build" ^
  "admin\portal_admin.pyw" || goto :error
echo.
echo Done: "%~dp0Portal Admin.exe"
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
