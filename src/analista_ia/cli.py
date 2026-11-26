"""Interface de linha de comando (CLI) do Analista de Dados com IA."""

from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from analista_ia.agent import AnalysisResult, DataAnalystAgent

console = Console()


def _render_result(result: AnalysisResult, save_chart: Path | None = None) -> None:
    if result.matched_metrics:
        console.print(
            f"[bold cyan]Métricas acionadas na camada semântica:[/] {', '.join(result.matched_metrics)}"
        )

    console.print(
        Panel(
            Syntax(result.sql_validated, "sql", theme="monokai", word_wrap=True),
            title=f"SQL Validado (provedor: {result.provider} | tentativas: {result.attempts})",
            border_style="blue",
        )
    )

    # Tabela Rich
    df = result.dataframe
    if df.empty:
        console.print("[yellow]Nenhum registro retornado.[/]")
    else:
        table = Table(title=f"Resultado ({len(df)} linha(s))", show_lines=False)
        for col in df.columns:
            table.add_column(str(col), style="white", overflow="fold")
        for _, row in df.head(20).iterrows():
            table.add_row(*["" if v is None else str(v) for v in row.tolist()])
        console.print(table)
        if len(df) > 20:
            console.print(f"[dim]Exibindo 20 de {len(df)} linhas.[/]")

    # Gráfico Plotly opcional salvo em HTML
    if result.chart is not None and save_chart is not None:
        save_chart.parent.mkdir(parents=True, exist_ok=True)
        result.chart.write_html(str(save_chart))
        console.print(f"[green]Gráfico interativo salvo em:[/] {save_chart}")
    elif result.chart is not None:
        console.print(
            "[dim]Gráfico gerado (use --save-chart outputs/grafico.html para salvar em HTML "
            "ou abra o app Streamlit).[/]"
        )

    # Interpretação executiva
    console.print(
        Panel(
            Markdown(result.interpretation),
            title="Interpretação Executiva",
            border_style="green",
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="analista-ia",
        description="Analista de Dados com IA — Text-to-SQL com camada semântica em DuckDB.",
    )
    parser.add_argument(
        "question",
        nargs="?",
        help="Pergunta de negócio em português (omitir para modo interativo).",
    )
    parser.add_argument(
        "--provider",
        choices=["anthropic", "openai", "gemini", "mock"],
        default=None,
        help="Sobrescreve LLM_PROVIDER do arquivo .env.",
    )
    parser.add_argument(
        "--save-chart",
        type=Path,
        default=None,
        help="Salva o gráfico gerado em arquivo HTML (ex.: outputs/grafico.html).",
    )
    args = parser.parse_args()

    try:
        agent = DataAnalystAgent.from_settings(provider_override=args.provider)
    except Exception as exc:
        console.print(f"[bold red]Erro ao inicializar agente:[/] {exc}")
        raise SystemExit(1) from exc

    if args.question:
        try:
            result = agent.ask(args.question)
            _render_result(result, save_chart=args.save_chart)
        except Exception as exc:
            console.print(f"[bold red]Erro ao processar pergunta:[/] {exc}")
            raise SystemExit(1) from exc
        return

    # Modo interativo
    console.print(
        Panel(
            "Faça perguntas de negócio em português sobre a carteira de seguros.\n"
            "Digite [bold]sair[/] para encerrar.",
            title=f"Analista de Dados com IA (provedor: {agent.llm_client.provider_name})",
            border_style="cyan",
        )
    )
    while True:
        try:
            question = console.input("\n[bold cyan]Pergunta > [/]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nEncerrando.")
            break
        if not question or question.lower() in {"sair", "exit", "quit"}:
            break
        try:
            result = agent.ask(question)
            _render_result(result, save_chart=args.save_chart)
        except Exception as exc:
            console.print(f"[bold red]Erro:[/] {exc}")


if __name__ == "__main__":
    main()
