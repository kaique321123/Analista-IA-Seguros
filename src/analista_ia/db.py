"""Executor somente leitura para consultas analíticas no DuckDB."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd


class DuckDBExecutor:
    """Executa consultas SQL no DuckDB em modo read_only=True."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"Banco DuckDB não encontrado em '{self.db_path}'. "
                "Execute `python data/generate_data.py` para gerar a base sintética."
            )

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Executa uma consulta SELECT já validada e retorna um DataFrame."""
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            return con.execute(sql).df()
        finally:
            con.close()

    def get_table_counts(self) -> dict[str, int]:
        """Retorna a contagem de registros por tabela para diagnóstico/UI."""
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            tables = [row[0] for row in con.execute("SHOW TABLES").fetchall()]
            return {
                t: int(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                for t in tables
            }
        finally:
            con.close()
