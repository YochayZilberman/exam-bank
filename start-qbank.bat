@echo off
rem Starts the Q bank site and opens it in the default browser.
rem Keep this window open while you use the site; close it to stop the site.
cd /d "%~dp0"
title Q bank
".venv\Scripts\python.exe" serve.py
pause
