@echo off
cd /d "%~dp0"
title Backtest

call .venv\Scripts\activate.bat
echo Running volatility breakout backtest (K sweep 0.1 - 0.9)...
python -m data.backtest 005930 000660 --sweep
pause
