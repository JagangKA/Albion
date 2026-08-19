@echo off
chcp 65001 >nul
title Albion - анализатор рынка
cd /d "%~dp0"

REM Ищем python: сначала лаунчер py, потом обычный python
where py >nul 2>nul && (set PY=py) || (set PY=python)

echo.
echo   Запускаю анализатор Albion...
echo   Браузер откроется сам. Это окно не закрывай - в нём работает приложение.
echo.

%PY% app.py
if errorlevel 1 (
  echo.
  echo   Не удалось запустить. Проверь, что установлен Python.
  pause
)
