# -*- coding: utf-8 -*-
"""Descifrado de las contraseñas de FBS que el hub guarda cifradas en Postgres
(fbs_conexiones.clave). Mismo formato que hub/cifrado.js:

    "v1:" + base64( iv[12] | tag[16] | datos )     AES-256-GCM, clave = HUB_CLAVE (32 bytes, base64)

Compatible hacia atrás: un valor sin el prefijo "v1:" es texto plano y se devuelve tal cual.
Se prueba HUB_CLAVE y, si existe, HUB_CLAVE_ANTERIOR (rotación). Los errores no incluyen la
clave ni el secreto. Necesita la misma HUB_CLAVE que el hub en el entorno de esta app.
"""
import base64
import binascii
import os

PREFIJO = "v1:"


class ErrorCifrado(RuntimeError):
    """No se pudo descifrar una contraseña guardada por el hub (mensaje sin secretos)."""


def esta_cifrado(valor) -> bool:
    return str(valor or "").startswith(PREFIJO)


def _clave(var: str):
    b64 = (os.environ.get(var) or "").strip()
    if not b64:
        return None
    try:
        k = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    return k if len(k) == 32 else None


def _con(clave: bytes, guardado: str):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        buf = base64.b64decode(guardado[len(PREFIJO):], validate=True)
        if len(buf) < 28:
            return None
        iv, tag, datos = buf[:12], buf[12:28], buf[28:]
        # cryptography espera datos + tag
        return AESGCM(clave).decrypt(iv, datos + tag, None).decode("utf-8")
    except Exception:  # noqa: BLE001 — tag inválido, base64 roto, utf-8 inválido
        return None


def descifrar_fbs(guardado) -> str:
    """Contraseña en claro. Texto plano (sin 'v1:') se devuelve igual."""
    g = "" if guardado is None else str(guardado)
    if not esta_cifrado(g):
        return g
    actual = _clave("HUB_CLAVE")
    anterior = _clave("HUB_CLAVE_ANTERIOR")
    if actual is None and anterior is None:
        raise ErrorCifrado(
            "La contraseña de FBS está cifrada por el hub y falta HUB_CLAVE (o no es de 32 bytes en base64) "
            "en esta app: poné la misma HUB_CLAVE que usa el hub.")
    for k in (actual, anterior):
        if k is not None:
            claro = _con(k, g)
            if claro is not None:
                return claro
    raise ErrorCifrado(
        "No se pudo descifrar la contraseña de FBS que guardó el hub: HUB_CLAVE (y HUB_CLAVE_ANTERIOR) "
        "no coinciden con las del hub, o el dato está dañado.")
