# ADR-0003: Usar el parámetro `?marca=` como contrato de integración con el hub

Fecha: 2026-09-04 · Estado: aceptado · Reconstruido el 2026-09-16 a partir del commit `2e8263a`

## Contexto

El grupo tiene once marcas y el conciliador arrancó atendiéndolas todas juntas. Con el modo diario y varias cuentas por marca, la pantalla mostraba cuentas que el operador de una concesionaria no tenía por qué ver. El hub, por su parte, ya tenía una convención propia para abrir cada aplicación posicionada en una marca [`[jira-OR.md]` OR-19].

Las alternativas eran un selector de marca dentro del conciliador, con su propio estado y su propia sesión, o aceptar la convención del hub tal como estaba.

## Decisión

La marca viaja en la URL, y nada más. Las dos páginas la leen del parámetro y la propagan a cada llamada; en el servidor filtra `/api/cuentas`, `/api/diario` y `/api/diario/identificar`, y etiqueta las corridas de `/api/conciliar` [`app.py:509-518,1117-1123`]. El comentario del código lo dice sin vueltas: "el hub puede linkear una página por marca" [`static/index.html:557-561`].

El match perdona. Se resuelve así: se normaliza sin acentos y se acepta por contención, así que `gac-kyoto` alcanza para `Gac - Kyoto Driving Automoviles SA` [`engine/cuentas.py:176-189`]. Si no hay coincidencia, el modo diario pide revisar el parámetro "o cargala en el hub".

## Consecuencias

La integración cuesta una línea de configuración. No hay código compartido entre el hub y el conciliador. El mismo patrón se pide para el Panel Comercial [`[jira-OR.md]` OR-19].

Lo que se perdió es cualquier garantía: el parámetro es una sugerencia de la URL, no un permiso. Cambiarlo a mano muestra otra marca, porque no hay autenticación detrás (ver ADR-0006).

Además, el filtro por marca alcanza a las cuentas y a las corridas, pero no al aprendizaje: reglas, equivalencias, gastos y vetos siguen siendo globales al servicio [`engine/reglas.py:21`].
