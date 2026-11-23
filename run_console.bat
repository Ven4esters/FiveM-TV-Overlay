@echo off
REM --- UTF-8 в консоли, иначе русский текст и эмодзи дают кракозябры ---
chcp 65001 >nul 2>&1
setlocal EnableExtensions
if not defined PYTHONIOENCODING set "PYTHONIOENCODING=utf-8"
if not defined PYTHONUTF8 set "PYTHONUTF8=1"
cd /d "%~dp0"

title GTA Video Host (console)

if not exist ".venv\Scripts\python.exe" (
    echo Окружение не найдено. Запустите сначала start.bat
    pause
    exit /b 1
)

echo Запускаю оверлей с выводом логов в это окно...
echo Закройте окно оверлея или нажмите Ctrl+C для выхода.
echo.

REM Код лежит в src/tvhost: запускаем ПАКЕТ (-m), а не файл overlay.py.
set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -m tvhost

echo.
echo Оверлей закрыт.
pause
