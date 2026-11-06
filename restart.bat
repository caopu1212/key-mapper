@echo off
cd /d "%~dp0"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'keymapper\.py') -or $_.Name -eq 'KeyMapper.exe' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
if "%1"=="build" del dist\KeyMapper.old.exe >nul 2>&1
rem a stuck old process may lock the exe: rename it first (a running exe can be renamed)
if "%1"=="build" if exist dist\KeyMapper.exe move /y dist\KeyMapper.exe dist\KeyMapper.old.exe >nul 2>&1
if "%1"=="build" python -m PyInstaller --noconfirm --onefile --windowed --name KeyMapper --icon "%~dp0icon.ico" --add-data "%~dp0icon.ico;." --distpath dist --workpath "%TEMP%\keymapper-build" --specpath "%TEMP%\keymapper-build" keymapper.py >nul 2>&1
if exist dist\KeyMapper.exe (start "" dist\KeyMapper.exe) else (start "" pythonw keymapper.py)
