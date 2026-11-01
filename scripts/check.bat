@echo off
cd /d "%~dp0.."
call .venv\Scripts\activate.bat 2>nul
ruff check app tests
pytest -q
