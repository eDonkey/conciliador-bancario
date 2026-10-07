@echo off
rem Backup de datos\ del conciliador. Agregar una linea que llame a este .bat
rem al backup nocturno del servidor (C:\backups\backup_hub.bat, tarea "Backup hub").
rem Uso: backup_datos.bat [carpeta_destino]   (por defecto C:\backups)
cd /d "%~dp0\.."
set DEST=%1
if "%DEST%"=="" set DEST=C:\backups
if exist venv\Scripts\python.exe (venv\Scripts\python.exe scripts\backup_datos.py --destino "%DEST%") else (python scripts\backup_datos.py --destino "%DEST%")
