# ADR-0006: Quitar la clave de acceso del conciliador a pedido

Fecha: 2026-07-29 · Estado: aceptado · Reconstruido el 2026-09-16 a partir de los commits `5acacad` y `6803948`

## Contexto

El 2026-07-28, un día después del primer commit, el conciliador salió a Railway con una protección por clave: la variable `CLAVE_ACCESO` y un chequeo sobre todas las rutas `/api`, que aceptaba la clave por el header `X-Clave` o por el parámetro `?clave=` [`5acacad`].

Al día siguiente esa protección estorbaba las pruebas del cliente.

## Decisión

Se sacó sin reemplazo. El mensaje del commit deja el motivo textual: "Quitar proteccion por clave de acceso (pedido: acceso libre para testing)" [`6803948`]. No se dejó nada en su lugar: ni clave, ni sesión, ni validación contra el hub. La decisión se tomó para una etapa de prueba y nunca se revisó.

## Consecuencias

Cincuenta días después el servicio sigue sin autenticación y ya no está en prueba: opera movimientos bancarios reales de todas las marcas del grupo. Quien tenga la URL puede listar y abrir cualquier corrida, borrarla con `DELETE /api/diario/{grupo_id}`, que hace `os.remove` y rebobina el arrastre, y bajarse el estado completo del servicio con `GET /api/migracion/exportar` [`app.py:449-488,1186-1222`].

Tampoco hay autoría: ningún registro guarda quién concilió a mano, quién anuló un cruce ni quién enseñó una regla [`app.py:1299,1386`].

El mismo problema está planteado para el Panel Comercial en `OR-21`, que pregunta si alcanza con no publicar la URL o hace falta un gate propio, y señala que el compensador sí tiene el suyo [`[jira-OR.md]` OR-21]. Recomendamos resolver los dos con el mismo criterio y en la misma semana. El costo de no hacerlo es que cualquier persona con el link opera la caja de todas las marcas.

Este ADR queda abierto: la decisión que lo reemplace debería escribirse como ADR nuevo.
