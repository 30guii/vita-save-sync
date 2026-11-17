@echo off
rem Gera "Vita Save Sync.exe" na pasta do projeto (precisa de: pip install pyinstaller pywebview)
cd /d "%~dp0.."
python -m PyInstaller --noconfirm --onefile --windowed --name "Vita Save Sync" ^
  --icon "%CD%\ui\icon.ico" --add-data "%CD%\ui;ui" ^
  --workpath "%CD%\build" --specpath "%CD%\build" --distpath "%CD%" ^
  "%CD%\PSVitaSaveSync.py"
rmdir /s /q "%CD%\build" 2>nul
echo.
echo Pronto: "%CD%\Vita Save Sync.exe"
pause
