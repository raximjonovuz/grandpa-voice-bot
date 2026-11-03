@echo off
title Grandpa Voice Bot
cd /d "%~dp0"
echo ====================================================
echo     👴 Instagram Grandpa Voice Bot ishga tushmoqda...
echo ====================================================
echo.
python main.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Xatolik yuz berdi. Tugmani bosing...
    pause
)
