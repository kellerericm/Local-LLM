@echo off
title LocalAgent
rem Starts LocalAgent and opens it in your browser. Close this window (or press Ctrl+C) to stop it.
cd /d "%~dp0"
"%USERPROFILE%\anaconda3\envs\local-llm\python.exe" -m localagent %*
if errorlevel 1 pause
