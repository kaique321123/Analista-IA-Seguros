"""Seleção e geração automática de gráficos Plotly a partir do resultado SQL."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

TIME_COL_HINTS = ("mes", "ano", "data", "trimestre", "semana", "periodo", "safra", "month", "year")


def _is_time_column(df: pd.DataFrame, col: str) -> bool:
    if pd.api.types.is_datetime64_any_dtype(df[col]):
        return True
    col_lower = col.lower()
    return any(hint in col_lower for hint in TIME_COL_HINTS)


def _format_label(col_name: str) -> str:
    return col_name.replace("_", " ").strip().capitalize()


def build_chart(df: pd.DataFrame, title: str | None = None) -> go.Figure | None:
    """Escolhe automaticamente um gráfico Plotly adequado ao formato do DataFrame.

    Retorna None quando o resultado é vazio, possui apenas 1 linha escalar (KPI)
    ou não contém colunas numéricas plotáveis.
    """
    if df is None or df.empty or len(df) < 2:
        return None

    numeric_cols = [
        c for c in df.columns
        if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])
    ]
    non_numeric_cols = [c for c in df.columns if c not in numeric_cols]

    if not numeric_cols:
        return None

    # Evita plotar IDs como métrica principal quando há outras colunas numéricas
    metric_cols = [c for c in numeric_cols if not c.lower().endswith("_id") and c.lower() != "id"]
    if not metric_cols:
        metric_cols = numeric_cols

    chart_title = (title[:80] + "...") if title and len(title) > 83 else (title or "Resultado Analítico")

    # 1. Série temporal: coluna de tempo + métrica(s)
    time_cols = [c for c in df.columns if _is_time_column(df, c)]
    if time_cols:
        x_col = time_cols[0]
        y_col = [c for c in metric_cols if c != x_col]
        if y_col:
            fig = px.line(
                df,
                x=x_col,
                y=y_col[0],
                markers=True,
                title=chart_title,
                labels={x_col: _format_label(x_col), y_col[0]: _format_label(y_col[0])},
            )
            fig.update_layout(template="plotly_white", hovermode="x unified")
            return fig

    # 2. Categórico x Numérico
    if non_numeric_cols:
        cat_col = non_numeric_cols[0]
        plot_df = df.head(20)

        # Se houver 2 categóricas e 1 numérica -> barras agrupadas por cor
        if len(non_numeric_cols) >= 2:
            color_col = non_numeric_cols[1]
            y_col = metric_cols[0]
            fig = px.bar(
                plot_df,
                x=cat_col,
                y=y_col,
                color=color_col,
                barmode="group",
                title=chart_title,
                labels={
                    cat_col: _format_label(cat_col),
                    y_col: _format_label(y_col),
                    color_col: _format_label(color_col),
                },
            )
            fig.update_layout(template="plotly_white")
            return fig

        # 1 categórica + 1 métrica principal (prioriza a última coluna, geralmente o KPI pedido)
        y_col = metric_cols[-1]
        horizontal = len(plot_df) > 6
        if horizontal:
            fig = px.bar(
                plot_df,
                x=y_col,
                y=cat_col,
                orientation="h",
                text_auto=".2s",
                title=chart_title,
                labels={cat_col: _format_label(cat_col), y_col: _format_label(y_col)},
            )
            fig.update_layout(yaxis={"categoryorder": "total ascending"})
        else:
            fig = px.bar(
                plot_df,
                x=cat_col,
                y=y_col,
                text_auto=".2s",
                title=chart_title,
                labels={cat_col: _format_label(cat_col), y_col: _format_label(y_col)},
            )
        fig.update_layout(template="plotly_white")
        return fig

    # 3. Apenas colunas numéricas -> dispersão entre as 2 primeiras
    if len(metric_cols) >= 2:
        fig = px.scatter(
            df.head(200),
            x=metric_cols[0],
            y=metric_cols[1],
            title=chart_title,
            labels={
                metric_cols[0]: _format_label(metric_cols[0]),
                metric_cols[1]: _format_label(metric_cols[1]),
            },
        )
        fig.update_layout(template="plotly_white")
        return fig

    return None
