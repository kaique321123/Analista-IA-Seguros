"""Testes de integridade da base sintética de seguros no DuckDB."""

from pathlib import Path

import duckdb
import pytest

from data.generate_data import generate_duckdb


@pytest.fixture(scope="module")
def sample_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db_path = tmp_path_factory.mktemp("duckdb") / "test_seguros.duckdb"
    generate_duckdb(db_path=db_path, n_clientes=400, n_apolices=900, seed=42)
    return db_path


def test_tables_exist_and_populated(sample_db: Path) -> None:
    con = duckdb.connect(str(sample_db), read_only=True)
    try:
        tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
        assert tables == {"clientes", "apolices", "renovacoes", "sinistros"}

        for table in tables:
            count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count > 0, f"Tabela {table} está vazia"
    finally:
        con.close()


def test_referential_integrity(sample_db: Path) -> None:
    con = duckdb.connect(str(sample_db), read_only=True)
    try:
        orphan_apolices = con.execute(
            """
            SELECT COUNT(*)
            FROM apolices a
            LEFT JOIN clientes c ON a.cliente_id = c.cliente_id
            WHERE c.cliente_id IS NULL
            """
        ).fetchone()[0]
        assert orphan_apolices == 0

        orphan_sinistros = con.execute(
            """
            SELECT COUNT(*)
            FROM sinistros s
            LEFT JOIN apolices a ON s.apolice_id = a.apolice_id
            WHERE a.apolice_id IS NULL
            """
        ).fetchone()[0]
        assert orphan_sinistros == 0

        orphan_renovacoes = con.execute(
            """
            SELECT COUNT(*)
            FROM renovacoes r
            LEFT JOIN apolices a ON r.apolice_id = a.apolice_id
            WHERE a.apolice_id IS NULL
            """
        ).fetchone()[0]
        assert orphan_renovacoes == 0
    finally:
        con.close()


def test_business_invariants(sample_db: Path) -> None:
    con = duckdb.connect(str(sample_db), read_only=True)
    try:
        # Prêmios sempre positivos
        neg_premio = con.execute(
            "SELECT COUNT(*) FROM apolices WHERE premio_emitido <= 0"
        ).fetchone()[0]
        assert neg_premio == 0

        # Sinistros não pagos devem ter valor_pago = 0
        invalid_pago = con.execute(
            "SELECT COUNT(*) FROM sinistros WHERE status_sinistro != 'Pago' AND valor_pago != 0"
        ).fetchone()[0]
        assert invalid_pago == 0

        # Renovações não elegíveis nunca podem constar como renovadas
        invalid_ren = con.execute(
            "SELECT COUNT(*) FROM renovacoes WHERE NOT elegivel_renovacao AND renovada"
        ).fetchone()[0]
        assert invalid_ren == 0

        # Taxa de renovação realista (entre 40% e 95%)
        taxa_ren = con.execute(
            """
            SELECT AVG(CASE WHEN renovada THEN 1.0 ELSE 0.0 END)
            FROM renovacoes
            WHERE elegivel_renovacao
            """
        ).fetchone()[0]
        assert 0.40 <= taxa_ren <= 0.95
    finally:
        con.close()


def test_deterministic_generation(tmp_path: Path) -> None:
    db1 = tmp_path / "db1.duckdb"
    db2 = tmp_path / "db2.duckdb"
    c1 = generate_duckdb(db_path=db1, n_clientes=100, n_apolices=200, seed=123)
    c2 = generate_duckdb(db_path=db2, n_clientes=100, n_apolices=200, seed=123)
    assert c1 == c2
