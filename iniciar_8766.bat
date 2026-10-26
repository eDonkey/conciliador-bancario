@echo off
rem Levanta ESTA copia (rama fbs-sql) en el puerto 8766, bajando antes lo que
rem haya quedado escuchando ahi (y en el 8765, el conciliador viejo).
cd /d "%~dp0"

echo Bajando lo que este escuchando en 8765 y 8766...
rem Ojo: uvicorn --reload son DOS procesos que comparten el socket. El taskkill
rem al PID que muestra netstat mataba solo a uno; el otro seguia vivo con el
rem puerto tomado y el codigo VIEJO en memoria, asi que el deploy "no entraba".
rem procesos.ps1 baja la familia entera y avisa si el puerto quedo ocupado.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\procesos.ps1" -Matar 8765
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\procesos.ps1" -Matar 8766

echo Instalando dependencias (solo la primera vez tarda)...
python -m pip install -r requirements.txt --quiet

echo.
echo Conciliador (rama fbs-sql) en http://%COMPUTERNAME%:8766/diario
echo Con AUTORECARGA: despues de un "git pull" el server se recarga solo
echo (solo si el pull trae dependencias nuevas hay que volver a correr este .bat).
echo Abajo de cada pantalla figura la version que esta corriendo.
start "" http://localhost:8766/diario
rem --reload vigila solo los .py (los json de datos/ no disparan recargas).
rem Ojo: --reload-exclude con globs no sirve en Windows (Click los expande).
python -m uvicorn app:app --host 0.0.0.0 --port 8766 --reload
