@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m PyInstaller --noconfirm --onefile --windowed --name KeyMapper --icon "%~dp0icon.ico" --add-data "%~dp0icon.ico;." --distpath dist --workpath "%TEMP%\keymapper-build" --specpath "%TEMP%\keymapper-build" keymapper.py
if not exist dist\keymap_config.json if exist keymap_config.json copy keymap_config.json dist\ >nul
echo.
echo 已生成 dist\KeyMapper.exe
pause
