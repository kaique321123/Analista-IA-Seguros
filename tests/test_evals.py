"""Testes do benchmark de avaliação (Evals) e da função de comparação de resultados."""

from pathlib import Path

import pandas as pd
import yaml

from analista_ia.config import PROJECT_ROOT
from analista_ia.db import DuckDBExecutor
from analista_ia.semantic import load_semantic_layer
from analista_ia.sql_validator import validate_and_prepare_sql
from data.generate_data import generate_duckdb
from evals.run_eval import compare_dataframes

BENCHMARK_PATH = PROJECT_ROOT / "evals" / "benchmark.yaml"


def test_all_benchmark_reference_queries_are_valid_and_return_data(tmp_path: Path) -> None:
    db_path = tmp_path / "eval_test.duckdb"
    generate_duckdb(db_path=db_path, n_clientes=500, n_apolices=1200, seed=42)
    executor = DuckDBExecutor(db_path)
    semantic = load_semantic_layer(PROJECT_ROOT / "semantic_layer")

    with BENCHMARK_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    cases = data["cases"]
    assert len(cases) >= 10

    for case in cases:
        prepared = validate_and_prepare_sql(
            case["expected_sql"],
            allowed_tables=semantic.table_names,
        )
        df = executor.execute_query(prepared)
        assert not df.empty, f"Gabarito do caso {case['id']} retornou vazio"


def test_compare_dataframes_allows_extra_context_columns_and_aliases() -> None:
    expected = pd.DataFrame({"ramo": ["Auto", "Vida"], "ticket_medio": [2800.0, 1450.0]})
    actual = pd.DataFrame(
        {
            "linha_produto": ["Auto", "Vida"],
            "qtd": [100, 50],
            "valor_medio": [2800.01, 1450.0],
        }
    )
    assert compare_dataframes(expected, actual) is True


def test_compare_dataframes_detects_value_mismatch() -> None:
    expected = pd.DataFrame({"ramo": ["Auto", "Vida"], "ticket_medio": [2800.0, 1450.0]})
    actual = pd.DataFrame({"ramo": ["Auto", "Vida"], "ticket_medio": [2800.0, 999.0]})
    assert compare_dataframes(expected, actual) is False
