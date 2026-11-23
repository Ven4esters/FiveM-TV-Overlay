@echo off
chcp 65001 >nul
rem Проверка одной ссылки: перетащите на этот файл ссылку из браузера
setlocal
set "PYTHONPATH=%~dp0src"
if "%~1"=="" (
  set /p LINK=Вставьте ссылку на видео и нажмите Enter: 
) else (
  set LINK=%~1
)
py -m tests.manual.check_link "%LINK%"
pause
