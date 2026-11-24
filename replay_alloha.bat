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

REM --- Ищем любой файл с командой: curl.txt, curl-2.txt, bnsi.txt и т.п. ---
set "CURLFILE="
if exist "alloha_check\curl.txt" set "CURLFILE=alloha_check\curl.txt"
if not defined CURLFILE (
    for %%F in ("alloha_check\curl*.txt") do (
        if not defined CURLFILE set "CURLFILE=%%~fF"
    )
)
if not defined CURLFILE (
    echo.
    echo [НУЖЕН ФАЙЛ] Положите "Copy as cURL" в папку alloha_check.
    echo Имя может быть любым: curl.txt, curl-2.txt и так далее.
    echo.
    echo 1. Откройте страницу с видео, дождитесь, пока плеер начнёт играть.
    echo 2. F12 - вкладка Network - в фильтре наберите: bnsi
    echo 3. Правой кнопкой по запросу bnsi - Copy - Copy as cURL
    echo 4. Блокнот, вставить, сохранить в папку alloha_check
    echo 5. Запустите этот файл снова.
    echo.
    pause
    exit /b 1
)
echo Файл с запросом: %CURLFILE%

echo.
echo ============================================================
echo   Повторяю запрос из %CURLFILE%
echo ============================================================
echo.

set "PYTHONPATH=%~dp0src"
"%PY%" -m tvhost2.alloha_replay "%CURLFILE%"
set "RESULT=%errorlevel%"

echo.
if "%RESULT%"=="0" (
    echo Запрос из браузера СРАБОТАЛ. Пришлите файл replay_*_body.txt из папки
    echo alloha_check — по нему видно, чего не хватает нашему запросу.
) else (
    echo Запрос завершился не с нулём. Пришлите всё, что выше: разберу.
)

REM --- Контрольный повтор тем же curl.exe, что встроен в Windows 10/11. ---
REM Нужен, чтобы понять: сервер различает КЛИЕНТА (TLS/HTTP2) или отвечает
REM 404 одинаково всем. Сравнив два ответа, сразу видно, где искать.
echo.
echo ============================================================
echo   Контроль: тот же запрос через curl.exe из Windows
echo ============================================================
where curl.exe >nul 2>&1
if errorlevel 1 (
    echo [пропуск] curl.exe не найден — это нормально для Windows 7/8.
) else (
    echo Код ответа curl.exe:
    curl.exe -K "alloha_check\replay_curl_request.txt" --silent --show-error --output "alloha_check\replay_curl_win_body.txt" --write-out "%%{http_code}  (размер %%{size_download} байт)\n"
    echo.
    echo Тело ответа curl.exe:
    type "alloha_check\replay_curl_win_body.txt"
    echo.
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
