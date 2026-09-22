---
target: conciliador fbs-sql (mensual + diario)
total_score: 24
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 3
timestamp: 2026-09-22T13-44-54Z
slug: static-index-html
---
Method: dual-agent (A: revisión de diseño · B: detector + navegador)

## Design Health Score

| # | Heurística | Puntaje | Hallazgo clave |
|---|---|---|---|
| 1 | Visibilidad del estado | 3 | El overlay mensual es el mejor momento de la app. Pero un botón secundario deshabilitado se ve idéntico a uno habilitado, y el diario concilia con un spinner sin fase ni ETA. |
| 2 | Coincidencia con el mundo real | 2 | Solapas "Conciliados (E)", "O pendientes sin banco", "Gastos por mes (ND)" sin glosario en el mensual. Columna "Confirmado en FES", un typo que inventa un sistema. |
| 3 | Control y libertad | 2 | "Anular cruce" crea un veto permanente sin ningún lugar donde verlo ni revertirlo. Del tablero diario no se vuelve al paso 2. |
| 4 | Consistencia y estándares | 2 | Mensual concilia con overlay, diario con spinner; tres clases distintas para el cuerpo de un diálogo; dos estilos de botón para "Confirmar" y "Confirmado". |
| 5 | Prevención de errores | 2 | Excel subido pisa la cuenta FBS en silencio; "elegir cuenta / ignorar" en una sola opción; conciliar con IA sin confirmación de costo. |
| 6 | Reconocimiento antes que recuerdo | 2 | Equivalencias y gastos viven en el header, lejos de la tabla; badges del panel manual sin leyenda; explicaciones solo en `title`. |
| 7 | Flexibilidad y eficiencia | 3 | Flechas en solapas, "según selección", Confirmar todos, retomar, link compartible. Sin atajo para conciliar selección. |
| 8 | Estético y minimalista | 2 | Hasta nueve avisos, diez KPIs iguales y once solapas antes de tablas de doce columnas. Párrafos del diario a 160-186 caracteres por línea. |
| 9 | Recuperación de errores | 3 | Avisos con `role="alert"` persistentes y overlay con Cerrar; los mensajes son el texto crudo del servidor sin siguiente paso. |
| 10 | Ayuda y documentación | 3 | El diario tiene glosarios; el mensual, con el triple de jerga, ninguno. El diálogo de conexión FBS habla en SQL. |
| | **Total** | **24/40** | **Aceptable** |

## Veredicto de especificidad

Capa visual con identidad (la del hub, sostenida en los dos temas). Composición e interacción intercambiables: dos dropzones y un botón, tres pasos y un dropzone. La idea fuerte del producto, "lo que no cruza hoy se arrastra a mañana", no tiene ningún componente que la dibuje; vive en párrafos. Lo único con carácter propio es el panel manual, enterrado como solapa 9 de 11.

Detector CLI (degradado): 8 hallazgos que son 3 hechos: Inter, grilla del hub, rayas en castellano (falso positivo). Overlays: en el mensual 6 de 7 apuntan a elementos ocultos (falsos positivos); el visible es el subtítulo en mayúsculas. En el diario, tres párrafos visibles a 162-186 caracteres por línea son reales.

## Problemas prioritarios

1. **[P1] Botón secundario deshabilitado invisible.** No existe `button.secundario:disabled`. "Traer E y O del FBS" deshabilitado es idéntico a "Configurar conexión" habilitado; también afecta a Excluir y Limpiar. Fix: regla `:disabled` con fondo apagado, texto `--texto2` y `cursor: not-allowed`, en los dos temas.
2. **[P1] El resultado del mensual no tiene punto de entrada.** Nueve KPIs iguales, hasta nueve avisos y once solapas de golpe. Fix: tres bloques (Explicado / Falta resolver con botón "Resolver a mano" / Esta corrida), y los avisos informativos plegados en un `details`.
3. **[P1] "Anular cruce" deja un veto permanente sin lugar donde verlo ni revertirlo.** Fix: sección "Cruces anulados" en la solapa de excluidos con "Volver a permitir", y el aviso de vetos linkeando ahí.
4. **[P2] El diario concilia sin overlay.** Spinner de texto, sin ETA, sin retomar. Fix: reutilizar el overlay del mensual con fase por cuenta y guardar el último grupo para retomar.
5. **[P2] Jerga sin glosario en el mensual y el typo FES.** Fix: "Confirmado en FBS"; glosario bajo la tabla que cambie con la solapa activa; solapas nombradas por la acción.
6. **[P2] Medida de texto en el diario.** Tres párrafos a 160-186 caracteres por línea en escritorio. Fix: `max-width: 72ch` en los textos explicativos y en el dropzone.
7. **[P3] Header del celular y costo de la IA.** Cuatro botones de igual peso antes de la tarea; el span ámbar del costo queda como tercera columna del checkbox. Fix: Equivalencias y Gastos bajan a la tarjeta en celular; el costo pasa al mismo nodo de texto.

## Banderas rojas por persona

- **Alex (diario, 7 cuentas):** botón FBS deshabilitado sin señal; "Identificar archivos" entre el paso 1 y el paso 2; fechas que arrancan en hoy sin preset "ayer y hoy"; para agregar un extracto olvidado hay que recargar; "Eliminar" rojo a 8px de "Abrir tablero".
- **Jordan (primera conciliación):** once solapas con E, O y ND sin explicar; "FES"; seis verbos para mover cosas explicados solo por `title`; signos y colores del panel manual sin leyenda.
- **Sam (teclado, baja visión):** orden de Tab y diálogos correctos; el h2 "Memoria diaria" anuncia el botón adentro; etiquetas de 11px en mayúsculas donde está la información que define cada número; botones de fila de 32px y de análisis de 28px.
- **Casey (link al tablero):** diez columnas sin totales ni semáforo; "Borrar memoria" es lo más visible debajo; sin vista de solo lectura; error de link expirado sobre la pantalla de carga.

## Observaciones menores

`td.num` muestra el cero como celda vacía; el bloque FBS anidado en el dropzone del mayor; filtrado de archivos por extensión en silencio; "Copiar link" sin anuncio para lector de pantalla; el diálogo Transferir no dice en el título de dónde a dónde; `.tarjeta` como cuerpo del diálogo de conexión; "Modo claro" y "Modo diario" empiezan igual y hacen cosas distintas; scroll anidado en el mensual y de página en el diario.

## Preguntas

1. Si el contador solo puede ver una cifra al terminar, ¿es "% explicado" o "cuánta plata falta explicar"?
2. ¿Por qué el panel manual es la solapa 9 de 11 en vez de la vista por defecto cuando hay pendientes?
3. Diario y mensual comparten tokens pero no overlay, glosario ni avisos. ¿Son dos productos o dos entradas al mismo?
4. Equivalencias, gastos, excepciones, reglas, vetos y análisis IA son seis memorias con cuatro diálogos y ningún lugar donde verlas juntas. ¿Qué aparece si hubiera una sola vista "Lo que el conciliador aprendió"?
