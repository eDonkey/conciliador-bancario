"""La protección de solo lectura de las queries del FBS (engine/fbs_sql.py): una sola sentencia SELECT/WITH,
nada escondido."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytest.importorskip("openpyxl")  # engine.fbs_sql importa el parser del mayor

from engine import fbs_sql  # noqa: E402


@pytest.mark.parametrize("query", [
    "UPDATE DetMov SET DetDebe = 1",
    "SELECT 1; DROP TABLE x",
    "SELECT * INTO copia FROM DetMov",
    "EXEC sp_who",
    # escondido detrás de un '--' o una comilla dentro de un texto o un identificador
    "SELECT 'a--', 1; DROP TABLE x",
    "SELECT [it's] FROM t; DROP TABLE x; SELECT [a']",
    'SELECT "a--" FROM t; DROP TABLE x',
    "SELECT 1 /* /* */ */ ; DROP TABLE t",
    "SELECT 1 /* /* */ ; DROP TABLE t",
    "SELECT 'sin cerrar",
    # control de transacciones y de sesión, que T-SQL corre sin ';'
    "SELECT 1 COMMIT",
    "SELECT 1 SET ROWCOUNT 0",
    "SELECT 1 WAITFOR DELAY '00:10:00'",
    # hints de bloqueo
    "SELECT * FROM DetMov WITH (TABLOCKX)",
    "SELECT * FROM DetMov WITH (UPDLOCK, HOLDLOCK)",
    "SELECT * FROM DetMov (XLOCK)",
])
def test_rechaza(query):
    with pytest.raises(ValueError):
        fbs_sql._validate_read_only(query)


@pytest.mark.parametrize("query", [
    "SELECT DetMovNro AS asiento FROM DetMov WHERE DetRef LIKE '%;drop%'",
    "SELECT [Detalle de Gestoria].Cuenta FROM [Detalle de Gestoria] WITH (NOLOCK) -- comentario",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    # comentarios /* */ anidados como en SQL Server: todo es comentario
    "SELECT 1 /* /* */ ; DROP TABLE t; /* */ */",
])
def test_acepta(query):
    fbs_sql._validate_read_only(query)
