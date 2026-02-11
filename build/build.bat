@echo off
REM Сборка 1CParserPFF в dist
REM Требует: pip install pyinstaller

cd /d "%~dp0.."
python build\build.py %*
