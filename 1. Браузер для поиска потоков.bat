@echo off
chcp 65001 >nul
title Браузер для поиска потоков (не закрывайте это окно)

rem ===================================================================
rem  Этот ярлык запускает Chrome с включённым отладочным портом.
rem  Нужен только для поиска потоков: приложение подключается к нему
rem  и забирает ссылки .m3u8, которые строит плеер сайта.
rem
rem  ВАЖНО: если Chrome уже открыт — закройте его полностью,
rem  иначе порт не освободится и поиск не сработает.
rem ===================================================================

set "PORT=9222"
set "PROFILE=%~dp0browser-profile"

set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "CHROME=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "CHROME=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"

if not defined CHROME (
    echo.
    echo   Не найден Chrome или Edge.
    echo   Установите Chrome — без него поиск потоков не работает.
    echo.
    pause
    exit /b 1
)

echo.
echo   Запускаю браузер для поиска потоков...
echo.
echo   1. Откройте страницу с видео и включите нужную озвучку.
echo   2. Нажмите в приложении «Найти поток на странице».
echo   3. Это окно закрывать не нужно — оно закроется само,
echo      когда вы закроете браузер.
echo.

start "Поиск потоков" "%CHROME%" ^
  --remote-debugging-port=%PORT% ^
  --user-data-dir="%PROFILE%" ^
  --no-first-run ^
  --no-default-browser-check ^
  --disable-features=Translate ^
  about:blank

echo   Браузер запущен. Порт отладки: %PORT%
timeout /t 3 >nul
