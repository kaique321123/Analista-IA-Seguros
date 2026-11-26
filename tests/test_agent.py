"""Testes ponta a ponta do agente Text-to-SQL com LLM mockado."""

from pathlib import Path

import pytest

from analista_ia.agent import DataAnalystAgent
from analista_ia.config import PROJECT_ROOT
from analista_ia.db import DuckDBExecutor
from analista_ia.llm import LLMClient, MockLLMClient
from analista_ia.semantic import load_semantic_layer
from data.generate_data import generate_duckdb


@pytest.fixture(scope="module")
def agent_env(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, DuckDBExecutor]:
    db_path = tmp_path_factory.mktemp("agent_db") / "seguros.duckdb"
    generate_duckdb(db_path=db_path, n_clientes=400, n_apolices=900, seed=42)
    return db_path, DuckDBExecutor(db_path)


def test_agent_end_to_end_sinistralidade(agent_env: tuple[Path, DuckDBExecutor]) -> None:
    _, executor = agent_env
    semantic = load_semantic_layer(PROJECT_ROOT / "semantic_layer")
    agent = DataAnalystAgent(
        semantic_layer=semantic,
        db_executor=executor,
        llm_client=MockLLMClient(),
    )

    result = agent.ask("Qual a sinistralidade por ramo?")
    assert "Sinistralidade (%)" in result.matched_metrics
    assert "LIMIT" in result.sql_validated.upper()
    assert not result.dataframe.empty
    assert "ramo" in result.dataframe.columns
    assert result.chart is not None
    assert "Resumo Executivo" in result.interpretation
    assert result.attempts == 1


def test_agent_end_to_end_renovacao_por_canal(agent_env: tuple[Path, DuckDBExecutor]) -> None:
    _, executor = agent_env
    semantic = load_semantic_layer(PROJECT_ROOT / "semantic_layer")
    agent = DataAnalystAgent(
        semantic_layer=semantic,
        db_executor=executor,
        llm_client=MockLLMClient(),
    )

    result = agent.ask("Qual a taxa de renovação e churn por canal de venda?")
    assert "Taxa de Renovação (%)" in result.matched_metrics
    assert "Taxa de Churn (%)" in result.matched_metrics
    assert not result.dataframe.empty
    assert result.chart is not None


class _FlakyLLM(LLMClient):
    """Retorna SQL inválido/perigoso na 1ª tentativa e SQL válido na 2ª."""

    provider_name = "flaky-test"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = 0.0) -> str:
        if "INTERPRETACAO_EXECUTIVA" in system_prompt:
            return "Interpretação após auto-correção."
        self.calls += 1
        if self.calls == 1:
            return "DROP TABLE apolices"
        return "SELECT ramo, ROUND(AVG(premio_emitido), 2) AS ticket_medio FROM apolices GROUP BY ramo"


def test_agent_self_corrects_after_validation_error(agent_env: tuple[Path, DuckDBExecutor]) -> None:
    _, executor = agent_env
    semantic = load_semantic_layer(PROJECT_ROOT / "semantic_layer")
    agent = DataAnalystAgent(
        semantic_layer=semantic,
        db_executor=executor,
        llm_client=_FlakyLLM(),
        max_retries=1,
    )

    result = agent.ask("Qual o ticket médio por ramo?")
    assert result.attempts == 2
    assert not result.dataframe.empty
    assert result.interpretation == "Interpretação após auto-correção."


class _AlwaysUnsafeLLM(LLMClient):
    provider_name = "unsafe-test"

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = 0.0) -> str:
        return "DELETE FROM apolices"


def test_agent_fails_when_all_attempts_are_unsafe(agent_env: tuple[Path, DuckDBExecutor]) -> None:
    _, executor = agent_env
    semantic = load_semantic_layer(PROJECT_ROOT / "semantic_layer")
    agent = DataAnalystAgent(
        semantic_layer=semantic,
        db_executor=executor,
        llm_client=_AlwaysUnsafeLLM(),
        max_retries=1,
    )

    with pytest.raises(RuntimeError, match="Não foi possível gerar/executar SQL válido"):
        agent.ask("Apague as apólices")
