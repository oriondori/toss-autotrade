@echo off
cd /d "%~dp0"
title Candle Collector

call .venv\Scripts\activate.bat
echo Collecting daily candles (3 years): 005930, 000660 ...
python -m data.collector 005930 000660
pause
