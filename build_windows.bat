@echo off
setlocal

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller

echo Building one-file Windows executable...
python -m PyInstaller --clean --noconfirm --onefile --console --name hlustyak_bot --add-data "welcome.png;." bot.py

echo.
echo Done: dist\hlustyak_bot.exe
pause