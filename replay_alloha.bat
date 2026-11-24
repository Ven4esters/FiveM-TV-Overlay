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
REM  Окружение проекта (.venv) и PYTHONPATH=src — как в start.bat.
REM ============================================================================
chcp 65001 >nul 2>&1
setlocal EnableExtensions
if not defined PYTHONIOENCODING set "PYTHONIOENCODING=utf-8"
if not defined PYTHONUTF8 set "PYTHONUTF8=1"
cd /d "%~dp0"
title Повтор запроса Alloha

set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" goto NO_VENV

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
"%VENV_PY%" -m tvhost2.alloha_replay "alloha_check\curl.txt"
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

:NO_VENV
echo.
echo [ОШИБКА] Не нахожу окружение проекта: %VENV_PY%
echo Запустите start.bat — он создаст .venv и поставит зависимости.
echo.
pause
exit /b 1
