# ADR-0001: Leer el FBS directo por SQL Server en vez de subir el mayor a mano

Fecha: 2026-09-07 · Estado: propuesto · Reconstruido el 2026-09-16 a partir de los commits `76f791e`, `1e4a143`, `bbc35bc` y `feb3990`

## Contexto

Hasta septiembre de 2026 el libro mayor entraba al conciliador como archivo: alguien abría el FBS, exportaba "Consulta del Mayor" a Excel y lo subía junto con el extracto [`parsers/mayor_xlsx.py:5-7`]. En el modo diario eso significa repetir la exportación todos los días, por cada cuenta y por cada marca. El FBS corre sobre SQL Server y la red del cliente lo alcanza, así que el dato estaba a una consulta de distancia.

El riesgo de conectarse es evidente: escribir por error en el sistema de gestión del cliente.

## Decisión

Leer el mayor con una consulta directa al SQL Server del FBS, en la rama `fbs-sql`, con tres capas de protección de solo lectura [`1e4a143`]. La primera valida la consulta antes de ejecutarla: una sola sentencia, que empiece en `SELECT` o `WITH`, sin punto y coma intermedio, con los comentarios y literales removidos antes de revisar, y una lista negra de unas treinta palabras. La segunda desactiva el autocommit y hace `rollback()` explícito, también en el camino de error. La tercera vive en el servidor. El login del FBS debe tener `db_datareader` más `db_denydatawriter`.

El par de cuentas contables E y O de cada cuenta bancaria vive en el hub, y la consulta se arma con un bloque por par unido con `UNION ALL` sobre la tabla `DetMov` [`bbc35bc`].

## Consecuencias

El mayor deja de subirse [`feb3990`]. Se ganó además una descarga en formato "Consulta del Mayor" para quien igual quiera el Excel [`f634c85`].

Se pierde portabilidad. Hacen falta drivers de SQL Server y estar en la red del cliente, lo que empujó la instalación on-premise en Windows con dos `.bat` de arranque, uno de ellos declarado TEMPORAL en su propio mensaje de commit [`b559d13`; `fec38d7`].

Queda pendiente lo más importante: la rama tiene 31 commits sin mergear a `master` y cinco arreglos aplicados dos veces, uno por rama [`git log master..origin/fbs-sql`].
