#!/usr/bin/env bash
# Version Linux de iniciar.bat: lanzador de desarrollo, doble click no aplica
# en Linux -> correr "bash iniciar.sh" (o "./iniciar.sh" si es ejecutable).
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "Creando entorno virtual (.venv)..."
    python3 -m venv .venv
fi

echo "Instalando dependencias (solo la primera vez tarda)..."
.venv/bin/pip install -r requirements.txt --quiet

echo ""
echo "Iniciando el conciliador en http://localhost:8765"
if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost:8765" >/dev/null 2>&1 &
fi
.venv/bin/python app.py
