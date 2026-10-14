import os

import pytest

from engine import entorno


@pytest.fixture
def env():
    return {}


def _archivo(tmp_path, texto, nombre="x.env"):
    p = tmp_path / nombre
    p.write_text(texto, encoding="utf-8")
    return str(p)


def test_lee_valores_comentarios_y_comillas(tmp_path):
    p = _archivo(tmp_path, "# comentario\n\nA=uno\nB = \"dos palabras\"\nC='tres'\nD=con#numeral\nE=\n")
    valores, invalidas = entorno.leer(p)
    assert valores == {"A": "uno", "B": "dos palabras", "C": "tres", "D": "con#numeral", "E": ""}
    assert invalidas == []


def test_acepta_set_export_y_bom(tmp_path):
    p = tmp_path / "bom.env"
    p.write_bytes("\ufeffset X=1\nexport Y=2\nSET Z=3\n".encode("utf-8"))
    valores, _ = entorno.leer(str(p))
    assert valores == {"X": "1", "Y": "2", "Z": "3"}


def test_lineas_invalidas_se_reportan_sin_valores(tmp_path):
    p = _archivo(tmp_path, "OK=1\nsin igual\n1MAL=2\n=3\n")
    valores, invalidas = entorno.leer(p)
    assert valores == {"OK": "1"} and invalidas == [2, 3, 4]


def test_cargar_completa_pero_no_pisa_el_entorno(tmp_path, env, capsys):
    env["CONCILIADOR_ENV"] = _archivo(tmp_path, "APP_PASSWORD=desde-archivo\nHUB_CLAVE=nueva\n")
    env["APP_PASSWORD"] = "del-entorno"
    est = entorno.cargar(env)
    assert env["APP_PASSWORD"] == "del-entorno" and env["HUB_CLAVE"] == "nueva"
    assert est["cargadas"] == ["HUB_CLAVE"] and est["respetadas"] == ["APP_PASSWORD"]
    salida = capsys.readouterr().out
    assert "HUB_CLAVE" in salida and "nueva" not in salida and "desde-archivo" not in salida


def test_variable_vacia_en_el_entorno_se_completa(tmp_path, env):
    env["CONCILIADOR_ENV"] = _archivo(tmp_path, "APP_PASSWORD=algo\n")
    env["APP_PASSWORD"] = ""
    entorno.cargar(env)
    assert env["APP_PASSWORD"] == "algo"


def test_archivo_inexistente_no_rompe(tmp_path, env, capsys):
    env["CONCILIADOR_ENV"] = str(tmp_path / "no-existe.env")
    est = entorno.cargar(env)
    assert est["archivo"] is None and est["cargadas"] == []
    assert "no existe" in capsys.readouterr().out


def test_sin_archivo_por_defecto_no_dice_nada(env, capsys, monkeypatch):
    monkeypatch.setattr(entorno, "ruta_por_defecto", lambda: os.path.join("Z:\\", "no", "hay.env"))
    est = entorno.cargar(env)
    assert est["archivo"] is None and capsys.readouterr().out == ""


def test_archivo_ilegible_no_rompe(tmp_path, env, capsys):
    p = tmp_path / "raro.env"
    p.write_bytes(b"\xff\xfe\x00\x80\x81 A=1")
    env["CONCILIADOR_ENV"] = str(p)
    entorno.cargar(env)          # no lanza
    assert "no se pudo leer" in capsys.readouterr().out


def test_ruta_por_defecto_es_carpeta_padre_mas_nombre():
    r = entorno.ruta_por_defecto()
    assert r.endswith(os.path.basename(entorno.BASE_DIR) + ".env")
    assert os.path.dirname(r) == os.path.dirname(entorno.BASE_DIR)


def test_estado_no_trae_valores(tmp_path, env):
    env["CONCILIADOR_ENV"] = _archivo(tmp_path, "APP_PASSWORD=secreto-123\n")
    entorno.cargar(env)
    assert "secreto-123" not in repr(entorno.estado())
