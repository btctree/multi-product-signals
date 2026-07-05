@echo off
cd /d "C:\Users\user\OneDrive\Desktop\Multi Product\engine"
python run_daily.py --update >> "..\reports\daily_run.log" 2>&1
