"""Testes de segurança do validador SQL (AST) e executor DuckDB read-only."""

from pathlib import Path

import pytest

from analista_ia.db import DuckDBExecutor
from analista_ia.sql_validator import SQLValidationError, validate_and_prepare_sql
from data.generate_data import generate_duckdb

ALLOWED = {"clientes", "apolices", "renovacoes", "sinistros"}


def test_valid_select_injects_default_limit() -> None:
    sql = "SELECT ramo, COUNT(*) AS total FROM apolices GROUP BY ramo"
    prepared = validate_and_prepare_sql(sql, allowed_tables=ALLOWED, default_limit=50, max_limit=200)
    assert "LIMIT 50" in prepared.upper()


def test_valid_cte_is_allowed_and_clamps_high_limit() -> None:
    sql = """
    WITH sin_agg AS (
        SELECT apolice_id, SUM(valor_pago) AS pago
        FROM sinistros
        GROUP BY apolice_id
    )
    SELECT a.ramo, SUM(COALESCE(s.pago, 0)) / SUM(a.premio_emitido) AS sinistralidade
    FROM apolices a
    LEFT JOIN sin_agg s ON a.apolice_id = s.apolice_id
    GROUP BY a.ramo
    LIMIT 9999
    """
    prepared = validate_and_prepare_sql(sql, allowed_tables=ALLOWED, default_limit=100, max_limit=250)
    assert "LIMIT 250" in prepared.upper()


def test_markdown_fences_are_stripped() -> None:
    sql = "```sql\nSELECT ramo FROM apolices LIMIT 5\n```"
    prepared = validate_and_prepare_sql(sql, allowed_tables=ALLOWED)
    assert "LIMIT 5" in prepared.upper()
    assert "```" not in prepared


@pytest.mark.parametrize(
    "malicious_sql",
    [
        "DROP TABLE apolices",
        "DELETE FROM apolices WHERE apolice_id = 1",
        "UPDATE apolices SET premio_emitido = 0",
        "INSERT INTO clientes VALUES (9999, 'PF', '18-29', 'Sul', 'PR', 'Varejo', '2025-01-01')",
        "CREATE TABLE hack AS SELECT * FROM apolices",
        "ALTER TABLE apolices ADD COLUMN x INT",
        "COPY apolices TO 'dump.csv'",
        "ATTACH 'other.db' AS other",
        "PRAGMA database_list",
        "SELECT * FROM apolices; DROP TABLE apolices;",
        "SELECT * FROM read_csv_auto('segredo.csv')",
        "SELECT * FROM duckdb_settings()",
        "",
    ],
)
def test_blocks_destructive_or_unsafe_sql(malicious_sql: str) -> None:
    with pytest.raises(SQLValidationError):
        validate_and_prepare_sql(malicious_sql, allowed_tables=ALLOWED)


def test_blocks_unauthorized_table() -> None:
    with pytest.raises(SQLValidationError, match="Tabela não autorizada"):
        validate_and_prepare_sql("SELECT * FROM usuarios_internos", allowed_tables=ALLOWED)


def test_duckdb_executor_runs_read_only_query(tmp_path: Path) -> None:
    db_path = tmp_path / "test.duckdb"
    generate_duckdb(db_path=db_path, n_clientes=100, n_apolices=200, seed=42)

    executor = DuckDBExecutor(db_path)
    counts = executor.get_table_counts()
    assert counts["apolices"] == 200

    sql = validate_and_prepare_sql(
        "SELECT ramo, COUNT(*) AS qtd FROM apolices GROUP BY ramo ORDER BY qtd DESC",
        allowed_tables=ALLOWED,
    )
    df = executor.execute_query(sql)
    assert not df.empty
    assert list(df.columns) == ["ramo", "qtd"]
