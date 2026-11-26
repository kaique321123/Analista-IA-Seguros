"""Runner de avaliação (Evals) para medir validade SQL e Execution Accuracy."""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml
from rich.console import Console
from rich.table import Table

from analista_ia.agent import DataAnalystAgent
from analista_ia.config import PROJECT_ROOT, get_settings
from analista_ia.db import DuckDBExecutor

DEFAULT_BENCHMARK_PATH = PROJECT_ROOT / "evals" / "benchmark.yaml"
console = Console()


@dataclass
class EvalCaseResult:
    case_id: str
    category: str
    question: str
    sql_valid: bool
    execution_match: bool
    error: str = ""


def _normalize_value(val: object) -> object:
    if pd.isna(val):
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return round(float(val), 2)
    return str(val).strip()


def _column_matches(expected_col: pd.Series, actual_col: pd.Series, tol: float = 0.05) -> bool:
    if len(expected_col) != len(actual_col):
        return False
    exp_vals = [_normalize_value(v) for v in expected_col.tolist()]
    act_vals = [_normalize_value(v) for v in actual_col.tolist()]

    for e, a in zip(exp_vals, act_vals):
        if isinstance(e, float) and isinstance(a, float):
            if not math.isclose(e, a, rel_tol=1e-3, abs_tol=tol):
                return False
        elif e != a:
            return False
    return True


def compare_dataframes(expected: pd.DataFrame, actual: pd.DataFrame) -> bool:
    """Verifica se o DataFrame gerado contém todas as colunas/valores do gabarito.

    Permite que o agente retorne colunas extras de contexto (ex.: quantidade_apolices
    além do KPI pedido) e nomes de alias diferentes, comparando por conteúdo após
    alinhar pela primeira coluna categórica quando aplicável.
    """
    if len(expected) != len(actual):
        return False
    if expected.empty and actual.empty:
        return True

    # Tenta alinhar ordem das linhas pela 1ª coluna (chave/dimensão) se houver correspondência
    exp_df = expected.reset_index(drop=True)
    act_df = actual.reset_index(drop=True)

    # Verifica se cada coluna do gabarito existe (por valor) em alguma coluna do resultado
    used_actual_cols: set[str] = set()
    for exp_col in exp_df.columns:
        matched = False
        for act_col in act_df.columns:
            if act_col in used_actual_cols:
                continue
            if _column_matches(exp_df[exp_col], act_df[act_col]):
                used_actual_cols.add(act_col)
                matched = True
                break
        if not matched:
            return False
    return True


def run_benchmark(
    benchmark_path: Path = DEFAULT_BENCHMARK_PATH,
    provider: str | None = None,
) -> list[EvalCaseResult]:
    with benchmark_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    cases = data.get("cases", [])
    settings = get_settings()
    executor = DuckDBExecutor(settings.resolved_duckdb_path)
    agent = DataAnalystAgent.from_settings(settings=settings, provider_override=provider)

    results: list[EvalCaseResult] = []
    for case in cases:
        expected_df = executor.execute_query(case["expected_sql"])
        try:
            agent_res = agent.ask(case["question"])
            match = compare_dataframes(expected_df, agent_res.dataframe)
            results.append(
                EvalCaseResult(
                    case_id=case["id"],
                    category=case["category"],
                    question=case["question"],
                    sql_valid=True,
                    execution_match=match,
                )
            )
        except Exception as exc:
            results.append(
                EvalCaseResult(
                    case_id=case["id"],
                    category=case["category"],
                    question=case["question"],
                    sql_valid=False,
                    execution_match=False,
                    error=str(exc),
                )
            )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Avalia acurácia do agente Text-to-SQL.")
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=DEFAULT_BENCHMARK_PATH,
        help="Caminho do arquivo benchmark.yaml.",
    )
    parser.add_argument(
        "--provider",
        choices=["anthropic", "openai", "gemini", "mock"],
        default=None,
        help="Provedor LLM para avaliação.",
    )
    args = parser.parse_args()

    results = run_benchmark(benchmark_path=args.benchmark, provider=args.provider)
    total = len(results)
    valid_count = sum(1 for r in results if r.sql_valid)
    match_count = sum(1 for r in results if r.execution_match)

    table = Table(title="Resultado da Avaliação (Text-to-SQL Benchmark)")
    table.add_column("Caso", style="cyan")
    table.add_column("Categoria")
    table.add_column("SQL Válido", justify="center")
    table.add_column("Execution Match", justify="center")
    table.add_column("Observação")

    for r in results:
        table.add_row(
            r.case_id,
            r.category,
            "[green]SIM[/]" if r.sql_valid else "[red]NÃO[/]",
            "[green]SIM[/]" if r.execution_match else "[yellow]NÃO[/]",
            r.error[:60] if r.error else "",
        )

    console.print(table)
    console.print(
        f"\n[bold]Resumo:[/] "
        f"SQL Válido: [cyan]{valid_count}/{total} ({valid_count / total * 100:.1f}%)[/] | "
        f"Execution Accuracy: [green]{match_count}/{total} ({match_count / total * 100:.1f}%)[/]"
    )


if __name__ == "__main__":
    main()
