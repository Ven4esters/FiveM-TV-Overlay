@echo off
REM --- Кодировка консоли: UTF-8, иначе русский текст превращается в кракозябры ---
REM 65001 = UTF-8. Если система не поддерживает, падать не должна (>nul 2>&1).
chcp 65001 >nul 2>&1
REM Переменные ставим ПОСЛЕ setlocal, иначе они потеряются.
setlocal EnableExtensions EnableDelayedExpansion
if not defined PYTHONIOENCODING set "PYTHONIOENCODING=utf-8"
if not defined PYTHONUTF8 set "PYTHONUTF8=1"
cd /d "%~dp0"

title GTA Video Host

REM ============================================================
REM   ЕДИНЫЙ ЗАПУСК:  start.bat
REM   Первый запуск  -> установка (.venv + зависимости) и старт
REM   Следующие      -> сразу запуск оверлея
REM ============================================================

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PYW=.venv\Scripts\pythonw.exe"

REM ---------- Проверка: окружение уже готово? ----------
if exist "%VENV_PYW%" (
    ".venv\Scripts\python.exe" -c "import yt_dlp, PySide6, imageio_ffmpeg" >nul 2>&1
    if not errorlevel 1 goto RUN
    echo Окружение найдено, но зависимости неполные. Доустанавливаю...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
    if errorlevel 1 goto FAIL
    goto RUN
)

REM ---------- ПЕРВЫЙ ЗАПУСК: установка ----------
echo ============================================================
echo   GTA Video Host - первый запуск (установка)
echo ============================================================
echo.

REM 1. Python
set "PYTHON="
where py >nul 2>&1
if %errorlevel%==0 set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>&1
    if %errorlevel%==0 set "PYTHON=python"
)
if not defined PYTHON (
    echo [ОШИБКА] Python не найден.
    echo.
    echo Скачайте Python 3.12: https://www.python.org/downloads/
    echo ВАЖНО: при установке отметьте "Add python.exe to PATH".
    echo После установки запустите start.bat снова.
    echo.
    pause
    exit /b 1
)

echo [1/4] Python найден:
%PYTHON% --version
echo.

echo [2/4] Создаю окружение .venv ...
%PYTHON% -m venv .venv
if errorlevel 1 goto FAIL
echo.

echo [3/4] Устанавливаю зависимости (yt-dlp, PySide6, imageio-ffmpeg) ...
echo       Это займёт 1-3 минуты, нужен интернет.
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto FAIL
echo.

echo [4/4] Проверяю установку ...
".venv\Scripts\python.exe" -c "import yt_dlp, PySide6; print('   OK: yt-dlp', yt_dlp.version.__version__, '/ PySide6', PySide6.__version__)"
if errorlevel 1 goto FAIL

REM Проверка вывода с эмодзи (частая причина сбоя на русской Windows)
REM Модуль лежит в пакете: src/tvhost/infra/console_io.py
set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -m tvhost.infra.console_io >nul 2>&1
if errorlevel 1 (
    echo    [предупреждение] Проблема с кодировкой консоли.
    echo    Прокси может не запуститься. Запустите: run_console.bat
) else (
    echo    OK: вывод с эмодзи работает
)
echo.

echo Установка завершена!
echo.
timeout /t 2 >nul

REM ---------- ЗАПУСК ----------
:RUN
REM Код лежит в src/tvhost: запускаем ПАКЕТ, а не файл overlay.py.
REM Без PYTHONPATH Python не найдёт пакет рядом со скриптом.
set "PYTHONPATH=%~dp0src"
echo Запускаю оверлей... (это окно закроется)
start "" "%VENV_PYW%" -m tvhost
exit /b 0

REM ---------- Ошибка ----------
:FAIL
echo.
echo [ОШИБКА] Установка не завершилась.
echo Проверьте интернет-соединение и запустите start.bat снова.
echo.
pause
exit /b 1
