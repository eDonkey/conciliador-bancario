"""The read-only protection of the FBS queries (engine/fbs_sql.py): a single SELECT/WITH statement,
nothing hidden."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytest.importorskip("openpyxl")  # engine.fbs_sql imports the ledger parser

from engine import fbs_sql  # noqa: E402


@pytest.mark.parametrize("query", [
    "UPDATE DetMov SET DetDebe = 1",
    "SELECT 1; DROP TABLE x",
    "SELECT * INTO copia FROM DetMov",
    "EXEC sp_who",
    # hidden behind a '--' or a quote inside a string or an identifier
    "SELECT 'a--', 1; DROP TABLE x",
    "SELECT [it's] FROM t; DROP TABLE x; SELECT [a']",
    'SELECT "a--" FROM t; DROP TABLE x',
    "SELECT 1 /* /* */ */ ; DROP TABLE t",
    "SELECT 1 /* /* */ ; DROP TABLE t",
    "SELECT 'sin cerrar",
    # transaction and session control, which T-SQL runs without ';'
    "SELECT 1 COMMIT",
    "SELECT 1 SET ROWCOUNT 0",
    "SELECT 1 WAITFOR DELAY '00:10:00'",
    # lock hints
    "SELECT * FROM DetMov WITH (TABLOCKX)",
    "SELECT * FROM DetMov WITH (UPDLOCK, HOLDLOCK)",
    "SELECT * FROM DetMov (XLOCK)",
])
def test_rejects(query):
    with pytest.raises(ValueError):
        fbs_sql._validate_read_only(query)


@pytest.mark.parametrize("query", [
    "SELECT DetMovNro AS asiento FROM DetMov WHERE DetRef LIKE '%;drop%'",
    "SELECT [Detalle de Gestoria].Cuenta FROM [Detalle de Gestoria] WITH (NOLOCK) -- comentario",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    # nested /* */ comments, as in SQL Server: all of it is a comment
    "SELECT 1 /* /* */ ; DROP TABLE t; /* */ */",
])
def test_accepts(query):
    fbs_sql._validate_read_only(query)
