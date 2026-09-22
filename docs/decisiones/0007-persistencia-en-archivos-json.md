# ADR-0007: Guardar todo el estado en archivos JSON en vez de una base propia

Fecha: 2026-07-28 · Estado: aceptado · Reconstruido el 2026-09-16 a partir del commit `2d4f94b` y de `app.py:78-90`

## Contexto

El conciliador nació como una herramienta de escritorio para una persona: subir dos archivos, mirar el resultado, exportar el Excel. Con ese alcance, una base de datos era una dependencia más para instalar en una máquina Windows del cliente [`iniciar.bat:1-8`].

## Decisión

Toda la persistencia son archivos JSON dentro de `datos/`, uno por concepto, leídos y escritos enteros en cada operación [`app.py:78-82`]. Hoy son trece de nombre fijo: el arrastre, la memoria de conciliados, los vetos, las reclasificaciones, las reglas aprendidas, las equivalencias, los gastos del usuario y sus excepciones, los aprendizajes y la caché del análisis, el uso diario, el log de IA y la copia local de cuentas. A eso se suma un archivo por conciliación y otro por tablero diario [`app.py:79,1111`].

No hay ORM, no hay esquema, no hay migraciones: cero [`find`].

## Consecuencias

El sistema arranca en cualquier lado con Python y sin instalar nada más, que era el objetivo. Copiar un servicio a otro es copiar una carpeta, y de ahí salieron `GET /api/migracion/exportar` y su contraparte de importación [`app.py:449-488`].

El costo es grande y sigue abierto. Sin el volumen montado en `/app/datos`, cada redeploy borra reglas, equivalencias, memoria, arrastre y conciliaciones [`README.md:85-86`]. No hay transacciones ni bloqueo, así que dos operaciones simultáneas sobre el mismo archivo se pisan. No hay índices: leer una lista de corridas abre todos los archivos. Y como la identidad de cada movimiento y de cada asiento es una firma de texto en vez de una clave, cualquier cambio en el formato del extracto cambia la firma y rompe la memoria [`app.py:694-712`].

Migrar a una base implicaría reescribir las trece funciones de carga y guardado de `app.py` y definir el esquema que hoy está implícito.
