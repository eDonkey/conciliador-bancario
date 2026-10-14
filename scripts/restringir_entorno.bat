@echo off
rem Deja el archivo de variables legible solo por SYSTEM, los administradores y el usuario actual.
rem Uso: scripts\restringir_entorno.bat C:\apps\conciladorfbs.env
if "%~1"=="" (
  echo Falta el archivo. Uso: restringir_entorno.bat C:\apps\conciladorfbs.env
  exit /b 1
)
if not exist "%~1" (
  echo No existe: %~1
  exit /b 1
)
icacls "%~1" /inheritance:r /grant:r *S-1-5-18:(F) *S-1-5-32-544:(F) "%USERNAME%:(R,W)" /remove *S-1-1-0 *S-1-5-32-545 *S-1-5-11
