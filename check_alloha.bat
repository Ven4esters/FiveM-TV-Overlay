@echo off
REM ============================================================================
REM  Проверка Alloha: пройти цепочку плеера и показать, где что ответило.
REM
REM  Зачем отдельный файл: обычный `python -m tvhost2.alloha_check` не
REM  работает, если пакет не установлен в Python — он лежит в src\, и Python
REM  про него не знает. Здесь всё уже настроено: берётся окружение проекта
REM  (.venv) и PYTHONPATH указывает на src\ — так же, как в start.bat.
REM
REM  Запуск: дважды щёлкнуть по этому файлу (спросит ссылку) либо
REM          check_alloha.bat "https://zlord.org/....html"
REM ============================================================================
chcp 65001 >nul 2>&1
setlocal EnableExtensions
if not defined PYTHONIOENCODING set "PYTHONIOENCODING=utf-8"
if not defined PYTHONUTF8 set "PYTHONUTF8=1"
cd /d "%~dp0"
title Проверка Alloha

set "VENV_PY=.venv\Scripts\python.exe"

REM --- 1. Окружение проекта ---
if not exist "%VENV_PY%" goto NO_VENV

REM --- 2. Ссылка: из аргумента или спросить ---
set "LINK=%~1"
if not defined LINK set /p "LINK=Вставьте ссылку на страницу с видео и нажмите Enter: "

if not defined LINK goto NO_LINK

echo.
echo ============================================================
echo   Проверяю: %LINK%
echo ============================================================
echo.

REM --- 3. Запуск диагностики ---
set "PYTHONPATH=%~dp0src"
"%VENV_PY%" -m tvhost2.alloha_check "%LINK%"
set "RESULT=%errorlevel%"

echo.
if "%RESULT%"=="0" (
    echo Проверка закончена: поток получен.
) else (
    echo Проверка закончена. Пришлите файл отчёта из папки alloha_check — по нему
    echo будет видно, на каком шаге Alloha отказала и с каким кодом.
)
echo.
pause
endlocal
exit /b 0

:NO_VENV
echo.
echo [ОШИБКА] Не нахожу окружение проекта: %VENV_PY%
echo.
echo Это значит, что приложение ещё ни разу не запускалось из этой папки.
echo Что делать: запустите start.bat — он создаст .venv и поставит зависимости,
echo после этого вернитесь к этому файлу.
echo.
pause
endlocal
exit /b 1

:NO_LINK
echo.
echo Ссылка не введена — проверять нечего.
echo.
pause
endlocal
exit /b 0
