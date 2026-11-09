@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка зависимостей ===
py -3 -m pip install --upgrade openrgb-python pystray pillow pyinstaller || goto :err
echo === Сборка RGBWave.exe ===
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin --name RGBWave rgb_wave.py || goto :err
copy /y config.json dist\config.json >nul
echo.
echo Готово! Файл: %~dp0dist\RGBWave.exe
pause
exit /b 0
:err
echo.
echo ОШИБКА сборки. Проверьте, что Python установлен с python.org (галочка "Add to PATH").
pause
exit /b 1
