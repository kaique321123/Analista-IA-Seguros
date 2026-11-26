"""Testes da camada semântica (YAMLs, integridade referencial e geração de contexto)."""

from pathlib import Path

import pytest

from analista_ia.config import PROJECT_ROOT
from analista_ia.semantic import load_semantic_layer

SEMANTIC_DIR = PROJECT_ROOT / "semantic_layer"


def test_load_semantic_layer_success() -> None:
    layer = load_semantic_layer(SEMANTIC_DIR)
    assert layer.table_names == {"clientes", "apolices", "renovacoes", "sinistros"}
    assert len(layer.relationships) >= 5

    metric_names = {m.name for m in layer.metrics}
    expected = {
        "premio_emitido_total",
        "ticket_medio",
        "sinistralidade",
        "taxa_renovacao",
        "taxa_churn",
    }
    assert expected.issubset(metric_names)


def test_find_relevant_metrics_matches_portuguese_synonyms() -> None:
    layer = load_semantic_layer(SEMANTIC_DIR)

    m1 = [m.name for m in layer.find_relevant_metrics("Qual a sinistralidade por ramo em 2024?")]
    assert "sinistralidade" in m1

    m2 = [m.name for m in layer.find_relevant_metrics("Qual o churn e a taxa de renovação por canal?")]
    assert "taxa_churn" in m2
    assert "taxa_renovacao" in m2

    m3 = [m.name for m in layer.find_relevant_metrics("Qual o ticket médio por segmento?")]
    assert "ticket_medio" in m3


def test_prompt_context_contains_schema_and_caveats() -> None:
    layer = load_semantic_layer(SEMANTIC_DIR)
    ctx = layer.to_prompt_context("Qual a sinistralidade por ramo?")
    assert "TABELAS E COLUNAS" in ctx
    assert "RELACIONAMENTOS (JOINS)" in ctx
    assert "MÉTRICAS OFICIAIS" in ctx
    assert "[RELEVANTE PARA A PERGUNTA]" in ctx
    assert "anti-fan-out" in ctx


def test_invalid_metric_table_raises_validation_error(tmp_path: Path) -> None:
    (tmp_path / "models.yaml").write_text(
        """
version: "1.0"
domain: teste
description: teste
tables:
  - name: apolices
    description: apolices
    grain: 1 por apolice
    primary_key: apolice_id
    columns:
      - name: apolice_id
        type: INTEGER
        description: id
""",
        encoding="utf-8",
    )
    (tmp_path / "metrics.yaml").write_text(
        """
version: "1.0"
metrics:
  - name: metrica_invalida
    label: Invalida
    description: Referencia tabela inexistente
    base_table: tabela_fantasma
    required_tables: ["tabela_fantasma"]
    unit: count
    formula_sql: "COUNT(*)"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="tabela_fantasma"):
        load_semantic_layer(tmp_path)
