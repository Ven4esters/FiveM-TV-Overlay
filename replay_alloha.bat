@echo off
REM ============================================================================
REM  Повтор настоящего запроса плеера: берём «Copy as cURL» из браузера и
REM  отправляем его один в один. Нужен, когда обычная проверка упирается в 404,
REM  хотя в браузере видео играет.
REM
REM  Как получить запрос:
REM    F12 -> Network -> фильтр «bnsi» -> правой кнопкой по запросу
REM    -> Copy -> Copy as cURL.  Вставить в alloha_check\curl.txt
REM
REM  Нужен только Python 3.10+ и PYTHONPATH=src: окружение проекта (.venv)
REM  берётся, если есть, иначе — обычный Python из установки Windows.
REM ============================================================================
chcp 65001 >nul 2>&1
setlocal EnableExtensions
if not defined PYTHONIOENCODING set "PYTHONIOENCODING=utf-8"
if not defined PYTHONUTF8 set "PYTHONUTF8=1"
cd /d "%~dp0"
title Повтор запроса Alloha

set "VENV_PY=.venv\Scripts\python.exe"

REM --- Чем запускать ---
REM --- Python: сначала окружение проекта, иначе системный ---
REM     Диагностика живёт только на стандартной библиотеке, поэтому .venv ей
REM     не обязателен: хватает обычного Python 3.10+ из установки Windows.
set "PY="
if exist "%VENV_PY%" set "PY=%VENV_PY%"
if not defined PY (
    python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if defined PY goto HAVE_PY
goto NO_PY
:HAVE_PY

if not exist "alloha_check\curl.txt" (
    echo.
    echo [НУЖЕН ФАЙЛ] alloha_check\curl.txt
    echo.
    echo 1. Откройте страницу с видео, дождитесь, пока плеер начнёт играть.
    echo 2. F12 - вкладка Network - в фильтре наберите: bnsi
    echo 3. Правой кнопкой по запросу bnsi - Copy - Copy as cURL
    echo 4. Откройте Блокнот, вставьте, сохраните как:
    echo       alloha_check\curl.txt
    echo 5. Запустите этот файл снова.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Повторяю запрос из alloha_check\curl.txt
echo ============================================================
echo.

set "PYTHONPATH=%~dp0src"
"%PY%" -m tvhost2.alloha_replay "alloha_check\curl.txt"
set "RESULT=%errorlevel%"

echo.
if "%RESULT%"=="0" (
    echo Запрос из браузера СРАБОТАЛ. Пришлите файл replay_*_body.txt из папки
    echo alloha_check — по нему видно, чего не хватает нашему запросу.
) else (
    echo Запрос завершился не с нулём. Пришлите всё, что выше: разберу.
)
echo.
pause
endlocal
exit /b 0

:NO_PY
echo.
echo [ОШИБКА] Не нахожу Python 3.10 или новее.
echo.
echo Диагностике не нужны библиотеки проекта — достаточно обычного Python.
echo 1. Откройте https://www.python.org/downloads/ и установите Python 3.12.
echo 2. При установке ОБЯЗАТЕЛЬНО поставьте галочку "Add python.exe to PATH".
echo 3. Закройте это окно и запустите файл снова.
echo.
echo Если Python уже есть, но Windows его не видит — запустите start.bat:
echo он найдёт Python сам и заодно создаст .venv.
echo.
pause
endlocal
exit /b 1
