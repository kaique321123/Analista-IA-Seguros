"""Interface web em Streamlit para o Analista de Dados com IA."""

from __future__ import annotations

import streamlit as st

from analista_ia.agent import DataAnalystAgent
from analista_ia.config import get_settings
from analista_ia.db import DuckDBExecutor
from analista_ia.semantic import load_semantic_layer

st.set_page_config(
    page_title="Analista de Dados com IA — Seguros",
    page_icon="📊",
    layout="wide",
)

EXAMPLE_QUESTIONS = [
    "Qual a sinistralidade por ramo?",
    "Qual a taxa de renovação e churn por canal de venda?",
    "Quais os principais motivos de não renovação?",
    "Qual o ticket médio por ramo?",
    "Qual a evolução mensal do prêmio emitido?",
]


def _default_provider_index(configured: str, has_key: bool) -> int:
    options = ["mock", "anthropic", "openai", "gemini"]
    if configured in options and (configured == "mock" or has_key):
        return options.index(configured)
    return 0


def main() -> None:
    settings = get_settings()
    semantic = load_semantic_layer(settings.resolved_semantic_dir)

    has_configured_key = (
        (settings.llm_provider == "anthropic" and bool(settings.anthropic_api_key))
        or (settings.llm_provider == "openai" and bool(settings.openai_api_key))
        or (settings.llm_provider == "gemini" and bool(settings.gemini_api_key))
    )

    with st.sidebar:
        st.header("Configurações")
        provider_options = ["mock", "anthropic", "openai", "gemini"]
        provider = st.selectbox(
            "Provedor de LLM",
            options=provider_options,
            index=_default_provider_index(settings.llm_provider, has_configured_key),
            help="Use 'mock' para demonstração offline sem chave de API, ou configure sua chave no .env.",
        )

        st.divider()
        st.subheader("Base Analítica (DuckDB)")
        try:
            executor = DuckDBExecutor(settings.resolved_duckdb_path)
            counts = executor.get_table_counts()
            for table_name, total in counts.items():
                st.caption(f"• `{table_name}`: **{total:,}** registros")
        except FileNotFoundError as exc:
            st.error(str(exc))
            st.stop()

        st.divider()
        st.subheader("Camada Semântica (YAML)")
        with st.expander(f"Métricas oficiais ({len(semantic.metrics)})", expanded=False):
            for m in semantic.metrics:
                st.markdown(f"**{m.label}** (`{m.name}`)")
                st.caption(m.description.strip())
                st.code(m.formula_sql.strip(), language="sql")
                if m.caveats:
                    st.caption(f"⚠️ {m.caveats.strip()}")
                st.markdown("---")

        with st.expander(f"Tabelas ({len(semantic.tables)})", expanded=False):
            for t in semantic.tables:
                st.markdown(f"**`{t.name}`** — {t.grain}")
                st.caption(", ".join(c.name for c in t.columns))

    st.title("📊 Analista de Dados com IA")
    st.caption(
        "Agente Text-to-SQL com camada semântica em YAML, validação AST (`sqlglot`) "
        "e execução somente leitura em DuckDB sobre uma base sintética de seguros."
    )

    # Sugestões rápidas
    st.markdown("**Perguntas sugeridas:**")
    cols = st.columns(len(EXAMPLE_QUESTIONS))
    selected_example: str | None = None
    for idx, q in enumerate(EXAMPLE_QUESTIONS):
        if cols[idx].button(q, key=f"ex_{idx}", use_container_width=True):
            selected_example = q

    if "history" not in st.session_state:
        st.session_state.history = []

    user_input = st.chat_input("Faça uma pergunta de negócio em português...")
    question = selected_example or user_input

    if question:
        with st.spinner("Consultando camada semântica, validando SQL e analisando resultados..."):
            try:
                agent = DataAnalystAgent.from_settings(
                    settings=settings,
                    provider_override=provider,
                )
                result = agent.ask(question)
                st.session_state.history.append({"ok": True, "result": result})
            except Exception as exc:
                st.session_state.history.append({"ok": False, "question": question, "error": str(exc)})

    for item in reversed(st.session_state.history):
        if not item["ok"]:
            with st.chat_message("user"):
                st.write(item["question"])
            with st.chat_message("assistant"):
                st.error(item["error"])
            continue

        res = item["result"]
        with st.chat_message("user"):
            st.write(res.question)

        with st.chat_message("assistant"):
            if res.matched_metrics:
                st.caption(
                    "Métricas da camada semântica acionadas: "
                    + " • ".join(f"`{m}`" for m in res.matched_metrics)
                )

            st.markdown(res.interpretation)

            if res.chart is not None:
                st.plotly_chart(res.chart, use_container_width=True)

            st.dataframe(res.dataframe, use_container_width=True)

            col_a, col_b = st.columns([3, 1])
            with col_a:
                with st.expander(
                    f"Ver SQL validado (provedor: {res.provider} | tentativas: {res.attempts})"
                ):
                    st.code(res.sql_validated, language="sql")
            with col_b:
                csv_bytes = res.dataframe.to_csv(index=False).encode("utf-8")
                st.download_button(
                    label="Baixar CSV",
                    data=csv_bytes,
                    file_name="resultado_analise.csv",
                    mime="text/csv",
                    key=f"dl_{id(res)}",
                )


if __name__ == "__main__":
    main()
