"""Carregamento, validação e consulta da camada semântica definida em YAML."""

from __future__ import annotations

import unicodedata
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


def _normalize_text(text: str) -> str:
    """Remove acentos e converte para minúsculas para busca lexical simples."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


class ColumnSpec(BaseModel):
    name: str
    type: str
    description: str
    values: list[str] = Field(default_factory=list)


class TableSpec(BaseModel):
    name: str
    description: str
    grain: str
    primary_key: str
    columns: list[ColumnSpec]

    @property
    def column_names(self) -> set[str]:
        return {col.name for col in self.columns}


class RelationshipSpec(BaseModel):
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    type: str


class MetricSpec(BaseModel):
    name: str
    label: str
    description: str
    base_table: str
    required_tables: list[str]
    unit: str
    formula_sql: str
    synonyms: list[str] = Field(default_factory=list)
    caveats: str = ""


class SemanticLayer(BaseModel):
    version: str
    dialect: str = "duckdb"
    domain: str
    description: str
    tables: list[TableSpec]
    relationships: list[RelationshipSpec] = Field(default_factory=list)
    metrics: list[MetricSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "SemanticLayer":
        table_map = {t.name: t for t in self.tables}

        for rel in self.relationships:
            if rel.from_table not in table_map:
                raise ValueError(f"Relacionamento referencia tabela inexistente: {rel.from_table}")
            if rel.to_table not in table_map:
                raise ValueError(f"Relacionamento referencia tabela inexistente: {rel.to_table}")
            if rel.from_column not in table_map[rel.from_table].column_names:
                raise ValueError(
                    f"Coluna inexistente em relacionamento: {rel.from_table}.{rel.from_column}"
                )
            if rel.to_column not in table_map[rel.to_table].column_names:
                raise ValueError(
                    f"Coluna inexistente em relacionamento: {rel.to_table}.{rel.to_column}"
                )

        for metric in self.metrics:
            if metric.base_table not in table_map:
                raise ValueError(
                    f"Métrica '{metric.name}' referencia base_table inexistente: {metric.base_table}"
                )
            for req in metric.required_tables:
                if req not in table_map:
                    raise ValueError(
                        f"Métrica '{metric.name}' referencia tabela inexistente: {req}"
                    )

        return self

    @property
    def table_names(self) -> set[str]:
        return {t.name for t in self.tables}

    def get_metric(self, name: str) -> MetricSpec | None:
        for metric in self.metrics:
            if metric.name == name:
                return metric
        return None

    def find_relevant_metrics(self, question: str) -> list[MetricSpec]:
        """Identifica métricas oficiais mencionadas ou sugeridas pela pergunta do usuário."""
        q_norm = _normalize_text(question)
        matches: list[MetricSpec] = []
        for metric in self.metrics:
            terms = [metric.name.replace("_", " "), metric.label, *metric.synonyms]
            if any(_normalize_text(term) in q_norm for term in terms if term):
                matches.append(metric)
        return matches

    def to_prompt_context(self, question: str | None = None) -> str:
        """Serializa schema, relacionamentos e métricas oficiais para o prompt do LLM."""
        lines: list[str] = [
            f"Dialeto SQL: {self.dialect}",
            f"Domínio: {self.domain} — {self.description.strip()}",
            "",
            "## TABELAS E COLUNAS",
        ]

        for table in self.tables:
            lines.append(
                f"### {table.name} (PK: {table.primary_key} | Grão: {table.grain})"
            )
            lines.append(f"Descrição: {table.description}")
            for col in table.columns:
                vals = f" | Valores: {', '.join(col.values)}" if col.values else ""
                lines.append(f"  - {col.name} ({col.type}): {col.description}{vals}")
            lines.append("")

        if self.relationships:
            lines.append("## RELACIONAMENTOS (JOINS)")
            for rel in self.relationships:
                lines.append(
                    f"  - {rel.from_table}.{rel.from_column} -> "
                    f"{rel.to_table}.{rel.to_column} ({rel.type})"
                )
            lines.append("")

        lines.append("## MÉTRICAS OFICIAIS DA CAMADA SEMÂNTICA")
        relevant = self.find_relevant_metrics(question) if question else []
        relevant_names = {m.name for m in relevant}

        for metric in self.metrics:
            tag = " [RELEVANTE PARA A PERGUNTA]" if metric.name in relevant_names else ""
            lines.append(f"### {metric.label} (`{metric.name}`){tag}")
            lines.append(f"  - Descrição: {metric.description.strip()}")
            lines.append(f"  - Unidade: {metric.unit} | Tabelas: {', '.join(metric.required_tables)}")
            lines.append(f"  - Fórmula SQL: {metric.formula_sql.strip()}")
            if metric.caveats:
                lines.append(f"  - Regra/Cuidado: {metric.caveats.strip()}")
            lines.append("")

        return "\n".join(lines).strip()


def load_semantic_layer(semantic_dir: Path) -> SemanticLayer:
    """Carrega e valida models.yaml e metrics.yaml do diretório informado."""
    models_path = semantic_dir / "models.yaml"
    metrics_path = semantic_dir / "metrics.yaml"

    if not models_path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {models_path}")
    if not metrics_path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {metrics_path}")

    with models_path.open("r", encoding="utf-8") as f:
        models_data = yaml.safe_load(f) or {}

    with metrics_path.open("r", encoding="utf-8") as f:
        metrics_data = yaml.safe_load(f) or {}

    merged = {
        **models_data,
        "metrics": metrics_data.get("metrics", []),
    }
    return SemanticLayer.model_validate(merged)
