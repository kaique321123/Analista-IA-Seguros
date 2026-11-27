"""Orquestrador do agente Text-to-SQL com camada semântica e interpretação executiva."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go

from analista_ia.charts import build_chart
from analista_ia.config import Settings, get_settings
from analista_ia.db import DuckDBExecutor
from analista_ia.guardrails import validate_user_question
from analista_ia.llm import LLMClient, create_llm_client
from analista_ia.semantic import SemanticLayer, load_semantic_layer
from analista_ia.sql_validator import SQLValidationError, validate_and_prepare_sql

SQL_SYSTEM_PROMPT = """Você é um Engenheiro Analítico Sênior especialista em seguros e DuckDB SQL.
Sua tarefa é converter a pergunta de negócio do usuário em UMA única consulta SQL válida para DuckDB.

REGRAS OBRIGATÓRIAS:
1. Consulte e obedeça rigorosamente as definições de tabelas, valores permitidos e fórmulas da CAMADA SEMÂNTICA abaixo.
2. Retorne APENAS a consulta SQL (sem explicações, sem comentários longos, sem texto antes ou depois).
3. Use somente instruções SELECT (pode usar CTEs com WITH). Nunca use DDL/DML.
4. Atenção especial à regra anti-fan-out: ao calcular sinistralidade ou cruzar apolices com sinistros, agregue sinistros por apolice_id (ou pela dimensão) em uma CTE antes do JOIN para não duplicar premio_emitido.
5. Ao calcular taxa de renovação ou churn, filtre sempre elegivel_renovacao = TRUE.
6. Use nomes de colunas limpos e descritivos no SELECT e ordene o resultado de forma útil para leitura executiva.
7. Respeite a grafia exata dos valores das colunas (ex.: 'Pago', 'Em Analise', 'Colisao', 'Preco' — sem acentos).
"""

INTERPRETATION_SYSTEM_PROMPT = """<!-- INTERPRETACAO_EXECUTIVA -->
Você é um Analista de Dados Sênior apresentando achados para liderança de negócios de uma seguradora.
Com base na pergunta, no SQL executado e na tabela de resultados, escreva uma interpretação executiva curta em português:
- Máximo de 3 a 4 bullets objetivos.
- Cite números concretos presentes na tabela (formate valores monetários em R$ e percentuais com %).
- Destaque o principal insight, eventual concentração ou risco e 1 sugestão prática de próximo passo.
- Nunca invente números que não estejam na tabela retornada.
"""


@dataclass
class AnalysisResult:
    """Resultado completo de uma pergunta respondida pelo agente."""

    question: str
    matched_metrics: list[str]
    sql_raw: str
    sql_validated: str
    dataframe: pd.DataFrame
    chart: go.Figure | None
    interpretation: str
    attempts: int
    provider: str


class DataAnalystAgent:
    """Agente Text-to-SQL com camada semântica, validação AST, execução e interpretação."""

    def __init__(
        self,
        semantic_layer: SemanticLayer,
        db_executor: DuckDBExecutor,
        llm_client: LLMClient,
        default_limit: int = 100,
        max_limit: int = 500,
        max_retries: int = 1,
        max_question_length: int = 240,
        max_question_words: int = 45,
    ) -> None:
        self.semantic_layer = semantic_layer
        self.db_executor = db_executor
        self.llm_client = llm_client
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.max_retries = max_retries
        self.max_question_length = max_question_length
        self.max_question_words = max_question_words

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        provider_override: str | None = None,
    ) -> "DataAnalystAgent":
        cfg = settings or get_settings()
        semantic = load_semantic_layer(cfg.resolved_semantic_dir)
        executor = DuckDBExecutor(cfg.resolved_duckdb_path)
        llm = create_llm_client(cfg, provider_override=provider_override)
        return cls(
            semantic_layer=semantic,
            db_executor=executor,
            llm_client=llm,
            default_limit=cfg.sql_default_limit,
            max_limit=cfg.sql_max_limit,
            max_question_length=cfg.max_question_length,
            max_question_words=cfg.max_question_words,
        )

    def _generate_sql(
        self,
        question: str,
        semantic_context: str,
        previous_error: str | None = None,
        previous_sql: str | None = None,
    ) -> str:
        user_parts = [
            "=== CAMADA SEMÂNTICA ===",
            semantic_context,
            "",
            f"=== PERGUNTA DE NEGÓCIO ===\n{question}",
        ]
        if previous_error and previous_sql:
            user_parts.extend(
                [
                    "",
                    "=== CORREÇÃO NECESSÁRIA ===",
                    f"Sua tentativa anterior de SQL:\n{previous_sql}",
                    f"Falhou com o seguinte erro:\n{previous_error}",
                    "Corrija a query mantendo estritamente as regras da camada semântica e retorne apenas o SQL.",
                ]
            )
        return self.llm_client.complete(
            system_prompt=SQL_SYSTEM_PROMPT,
            user_prompt="\n".join(user_parts),
            temperature=0.0,
        )

    def _interpret(self, question: str, sql_validated: str, df: pd.DataFrame) -> str:
        if df.empty:
            return (
                "A consulta foi executada com sucesso, mas não retornou registros para os filtros "
                "informados. Tente ampliar o período ou revisar os critérios da pergunta."
            )

        preview_df = df.head(15).copy()
        for col in preview_df.columns:
            if not pd.api.types.is_numeric_dtype(preview_df[col]):
                preview_df[col] = preview_df[col].astype(str).str.slice(0, 80)
        preview_csv = preview_df.to_csv(index=False)
        user_prompt = (
            f"Pergunta do usuário: {question}\n\n"
            f"SQL validado executado:\n{sql_validated}\n\n"
            f"Amostra dos resultados ({len(df)} linhas no total, exibindo até 15):\n{preview_csv}"
        )
        return self.llm_client.complete(
            system_prompt=INTERPRETATION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.2,
        )

    def ask(self, question: str) -> AnalysisResult:
        """Executa o fluxo completo: guardrails -> camada semântica -> SQL -> validação -> execução -> gráfico -> interpretação."""
        question_clean = validate_user_question(
            question,
            max_chars=self.max_question_length,
            max_words=self.max_question_words,
        )

        matched_metrics = [m.label for m in self.semantic_layer.find_relevant_metrics(question_clean)]
        semantic_context = self.semantic_layer.to_prompt_context(question_clean)

        last_error: str | None = None
        last_sql: str | None = None
        sql_validated = ""
        df = pd.DataFrame()
        attempts = 0

        for attempt in range(1, self.max_retries + 2):
            attempts = attempt
            raw_sql = self._generate_sql(
                question=question_clean,
                semantic_context=semantic_context,
                previous_error=last_error,
                previous_sql=last_sql,
            )
            last_sql = raw_sql

            try:
                sql_validated = validate_and_prepare_sql(
                    raw_sql,
                    allowed_tables=self.semantic_layer.table_names,
                    default_limit=self.default_limit,
                    max_limit=self.max_limit,
                )
                df = self.db_executor.execute_query(sql_validated)
                break
            except (SQLValidationError, Exception) as exc:
                last_error = str(exc)
                if attempt > self.max_retries:
                    raise RuntimeError(
                        f"Não foi possível gerar/executar SQL válido após {attempts} tentativa(s). "
                        f"Último erro: {last_error}"
                    ) from exc

        chart = build_chart(df, title=question_clean)
        interpretation = self._interpret(question_clean, sql_validated, df)

        return AnalysisResult(
            question=question_clean,
            matched_metrics=matched_metrics,
            sql_raw=last_sql or "",
            sql_validated=sql_validated,
            dataframe=df,
            chart=chart,
            interpretation=interpretation,
            attempts=attempts,
            provider=self.llm_client.provider_name,
        )
