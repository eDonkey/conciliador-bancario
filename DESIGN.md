---
name: Conciliador bancario
description: Herramienta operativa de conciliación bancaria (extractos vs. mayor FBS), comparte identidad visual con el hub del grupo
colors:
  acento: "#38bdf8"
  acento-claro: "#0a6fb5"
  texto-sobre-acento: "#06131f"
  fondo1: "#070b18"
  fondo2: "#0d1430"
  panel: "rgba(255, 255, 255, 0.055)"
  panel-2: "rgba(255, 255, 255, 0.04)"
  campo: "rgba(255, 255, 255, 0.05)"
  border: "rgba(255, 255, 255, 0.12)"
  texto: "#e8ecf8"
  texto2: "#94a0c2"
  rojo: "#ff8896"
  verde: "#34d399"
  ambar: "#fbbf24"
  violeta: "#c4b5fd"
  th-bg: "#10173a"
  velo: "rgba(4, 7, 18, 0.7)"
  dialogo: "#0e1637"
  hueco: "rgba(0, 0, 0, 0.16)"
  demo-franja: "#f59e0b"
  demo-tinta: "#1a1006"
  claro-fondo: "#e9edf4"
  claro-texto: "#17203a"
  claro-texto2: "#5b6880"
  claro-rojo: "#c0313d"
  claro-verde: "#0b7a54"
  claro-ambar: "#8a5a00"
  claro-violeta: "#6d4fc4"
  claro-th-bg: "#e9edf5"
typography:
  display:
    fontFamily: "Sora, sans-serif"
    fontSize: "16px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0.04em"
  kpi:
    fontFamily: "Sora, sans-serif"
    fontSize: "34px"
    fontWeight: 800
    lineHeight: 1.1
    letterSpacing: "normal"
  body:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  small:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  label:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: "0.08em"
rounded:
  xs: "4px"
  sm: "8px"
  md: "10px"
  lg: "12px"
  xl: "16px"
  modal: "20px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "14px"
  lg: "22px"
  xl: "28px"
components:
  button-primary:
    backgroundColor: "{colors.acento}"
    textColor: "{colors.texto-sobre-acento}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "12px 26px"
    height: "44px"
  button-secondary:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.texto}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "9px 16px"
    height: "40px"
  button-mini:
    backgroundColor: "{colors.verde}"
    textColor: "{colors.texto-sobre-acento}"
    typography: "{typography.small}"
    rounded: "{rounded.sm}"
    padding: "5px 13px"
  card:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.xl}"
    padding: "{spacing.lg}"
  input:
    backgroundColor: "{colors.campo}"
    textColor: "{colors.texto}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
  chip:
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "1px 8px"
  table-header:
    backgroundColor: "{colors.th-bg}"
    textColor: "{colors.texto2}"
    typography: "{typography.label}"
    padding: "8px 10px"
---

## Overview

Interfaz de trabajo para contadores: subir extractos, traer el mayor E y O del FBS (o subirlo), revisar el cruce, resolver a mano lo que quedó pendiente y exportar. Modo **Operate**: escaneo rápido de tablas densas, importes alineados, estados diferenciados por color y por texto. La identidad es la del hub del grupo: fondo azul noche con una grilla tenue, paneles translúcidos, un solo acento celeste, tipografía Sora para títulos y cifras e Inter para todo lo demás. Tema claro conmutable con la misma estructura.

Todos los tokens viven en el bloque `:root` de cada página y se redefinen en `body.claro`. Los dos archivos de `static/` deben mantener el mismo bloque de tokens; si se agrega uno, va en los dos.

## Colors

- **Un acento**: `--acento` para acciones primarias, foco, links, solapa activa y chips de "extracto". No se usa para texto de cuerpo.
- **Semánticos**: `--verde` conciliado y diferencia cero, `--ambar` pendiente de confirmar y avisos de simulación, `--rojo` sin contabilizar, errores y acciones destructivas (`.peligro`), `--violeta` chips de "FBS mayor". Nunca solo color: siempre acompañado de texto o etiqueta.
- **Neutros**: `--texto` para cuerpo, `--texto2` para secundario, etiquetas y cabeceras de tabla. `--panel` y `--panel-2` para superficies, `--campo` para inputs, `--hueco` para listas rebajadas, `--dialogo` para el cuerpo de diálogos y avisos flotantes, `--border` para todos los bordes.
- **Transparencias derivadas** del acento o del semántico con `color-mix(in srgb, var(--token) N%, transparent)`. Nunca `rgba()` de un color de marca escrito a mano.
- **Contraste**: todos los pares texto/fondo superan 4.5:1 en los dos temas. En tema claro los semánticos son versiones oscuras (`claro-*`), porque los del tema oscuro no llegan sobre blanco.
- **Texto sobre acento** es oscuro en tema oscuro y blanco en tema claro (`--texto-sobre-acento`, `--texto-sobre-verde`). El tope de los degradados (`--acento-tope`, `--verde-tope`) aclara en oscuro y oscurece en claro para no perder contraste.
- **Excepción**: la franja de demo (`.demo-banner`) usa ámbar y tinta fijos en los dos temas porque es una señal de peligro, no parte de la paleta.

## Typography

Escala de seis pasos en tokens `--fs-*`. No se escriben tamaños en píxeles fuera de los tokens.

| Token | Tamaño | Uso |
|---|---|---|
| `--fs-xs` | 11px | etiquetas en mayúsculas, chips, cabeceras de tabla, metadatos |
| `--fs-sm` | 12px | tablas, ítems de listas, texto explicativo secundario |
| `--fs-md` | 13px | cuerpo, botones secundarios, inputs |
| `--fs-lg` | 14px | botón principal, títulos de zona de carga |
| `--fs-xl` | 16px | h1, títulos de diálogos |
| cifra de bloque | 34px | la cifra grande de "Extracto explicado" y "Falta resolver" |

Sora 700/800 para h1, h3 de tarjetas y cifras. Inter 400/500/600 para el resto. Las etiquetas en mayúsculas llevan `letter-spacing: 0.08em`. Importes siempre con `font-variant-numeric: tabular-nums` y alineados a la derecha. Cuerpo con `line-height: 1.45`.

## Layout

- `main` de 1200px máximo con 20px de margen lateral, 16px en celular.
- Header pegajoso en escritorio, estático en celular (bajo 720px) para no comerse la pantalla; sus acciones viven en `.zona-derecha` y envuelven en dos columnas.
- Grillas de dos columnas (`.grid-carga`, `.dual`) colapsan a una bajo 760px y 900px. KPIs con `auto-fit, minmax(150px, 1fr)`.
- Tablas anchas viven dentro de `.contenedor-tabla` con scroll horizontal propio y cabecera pegajosa; la página nunca scrollea de costado.
- Objetivos táctiles: 40px de alto mínimo en botones, 44px en la acción principal y en las solapas, y 44px en todo control con `pointer: coarse`.

## Elevation & Depth

Profundidad por transparencia y borde, no por sombra. Paneles: `--panel` sobre el fondo con borde `--border`. Solo flotan el header (blur 14px), los diálogos (velo `--velo` con blur 8px y sombra `--sombra-modal`) y los avisos flotantes. Sin halos de color: el logo lleva un anillo del acento al 22%, los botones no llevan sombra.

## Shapes

Radios: 4px para la barra de progreso, 8px para inputs, chips rectangulares y botones de fila, 10px para botones y listas, 12px para zonas de carga, KPI y barras, 16px para tarjetas y paneles, 20px para diálogos, píldora para chips y contadores. Zonas de carga con borde punteado de 1px.

## Components

- **Zona de carga** (`.zona` con `.zona-cta` adentro): el bloque título más texto es el `role="button"` con `tabindex="0"`; Enter o Espacio abren el selector. Los controles anidados (cuenta y fechas del FBS en el mensual) quedan fuera del rol. Estado `arrastrando` y hover con borde y fondo del acento.
- **Botón principal** (`button.principal`): degradado vertical corto del acento, texto `--texto-sobre-acento`. Deshabilitado gris con `--texto2`.
- **Botón secundario** (`button.secundario`, `a.secundario`, `a.volver`): fondo `--panel-2`, borde `--border`. Variantes `.btn-chico` (34px) y `.btn-fila` (32px) para barras y celdas. `.peligro` para acciones destructivas: rojo al 10% con texto rojo.
- **Botón mini** (`button.mini`): verde, para confirmar dentro de un panel o diálogo.
- **Diálogos** (`.modal` + `.modal-card`, `.eq-card` o `.tarjeta`): `role="dialog"`, `aria-modal`, `aria-labelledby` al título. Se abren con `abrirModal(id, foco)` y se cierran con `cerrarModal(id)`; Escape pulsa el botón marcado `data-cerrar`, Tab queda atrapado y el foco vuelve al disparador. El overlay de progreso es un diálogo sin cierre hasta que termina o falla.
- **Confirmación** (`confirmar({ titulo, texto, ok, peligro })`): devuelve una promesa con `true` o `false`; foco inicial en Cancelar; con `peligro` el botón de acción es rojo. Reemplaza a `confirm()`.
- **Avisos flotantes** (`avisar(msg, 'error' | 'info')`): se apilan abajo al centro, los errores tienen `role="alert"` y quedan hasta cerrarse, los de información desaparecen solos. Reemplazan a `alert()` y al `prompt()` del portapapeles.
- **Solapas** (`nav.tabs`): `role="tablist"`, botones `role="tab"` con `aria-selected` y foco circular, flechas y Home/End cambian de solapa. Paneles `role="tabpanel"`.
- **Resumen de resultado** (`.resumen` con tres `.bloque`): Extracto explicado, Falta resolver (protagonista, con borde rojo o verde según haya pendientes y el botón "Resolver a mano") y Esta corrida. Una sola cifra grande por bloque; el resto en lista nombre/valor. Las notas informativas de la corrida van plegadas en `details.notas-corrida`; solo errores y avisos de marca quedan visibles.
- **Tablas**: `th scope="col"` en mayúsculas `--fs-xs`, filas de 12px con separador al 6%. Celdas numéricas `.num`.
- **Chips** (`.chip`, `.metodo`, `.pill-an`, `.badge`): píldora con fondo del color al 12% y borde al 35–40%.
- **Glosario** (`details.glosario`): explicación visible de columnas y estados debajo de cada tabla del diario, con chevron dibujado en CSS. El `title` de las cabeceras es un refuerzo, no la única fuente.
- **Regiones vivas**: `#estado`, `#estadoCarga`, `#estadoConciliar`, `#fbsSqlMsj`, `#fbsConfMsj`, `#ovFase` con `role="status"`; errores con `role="alert"`.

## Do's and Don'ts

- Usá los tokens; si un valor no existe, agregalo al `:root` de las dos páginas y a este archivo.
- Todo texto que venga del servidor pasa por `esc()` antes de entrar a `innerHTML`.
- Los mensajes de estado se escriben en las regiones vivas; los errores van por `avisar()`, no en gris de estado.
- Los diálogos pasan por `abrirModal` y `cerrarModal`, nunca por `classList` a mano.
- No uses `prompt()`, `confirm()` ni `alert()`: están `confirmar()` y `avisar()`.
- No animes `width` ni `height`; la barra de progreso escala con `transform`.
- No agregues tooltips como único lugar de una explicación: lo que importa va visible, el `title` es un refuerzo.
- No introduzcas sombras de color, texto en degradado ni `backdrop-filter` en tarjetas estáticas.
- El tema se decide antes del primer pintado con el script al inicio de `body`; no lo muevas al final.
- Nada de glifos Unicode como íconos: el chevron del glosario se dibuja con bordes, los links de descarga dicen "Descargar".
