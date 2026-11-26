"""Validação de segurança e saneamento de SQL via AST (sqlglot).

Camada de defesa em profundidade para garantir que apenas consultas analíticas
somente leitura (SELECT / WITH / UNION), restritas às tabelas autorizadas e com
limite de linhas, cheguem ao DuckDB.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


class SQLValidationError(ValueError):
    """Erro levantado quando o SQL viola regras de sintaxe ou segurança."""


# Nós AST explicitamente proibidos (DDL, DML, controle de transação e comandos de sistema)
_FORBIDDEN_NODE_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Drop",
    "Alter",
    "Create",
    "Command",
    "Copy",
    "Attach",
    "Detach",
    "Pragma",
    "Set",
    "Export",
    "LoadData",
    "Transaction",
    "Commit",
    "Rollback",
    "Grant",
    "Revoke",
    "Merge",
    "TruncateTable",
)
FORBIDDEN_NODE_TYPES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name) for name in _FORBIDDEN_NODE_NAMES if hasattr(exp, name)
)

# Funções DuckDB que acessam sistema de arquivos, rede ou metadados internos
FORBIDDEN_FUNCTIONS = {
    "read_csv",
    "read_csv_auto",
    "read_parquet",
    "read_json",
    "read_json_auto",
    "read_ndjson",
    "read_ndjson_auto",
    "read_text",
    "read_blob",
    "glob",
    "parquet_scan",
    "csv_scan",
    "sqlite_scan",
    "sqlite_attach",
    "postgres_scan",
    "postgres_attach",
    "duckdb_settings",
    "duckdb_secrets",
    "duckdb_extensions",
    "pragma_database_list",
    "getenv",
}

_FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*(.*?)\s*```\s*$", re.IGNORECASE | re.DOTALL)


def strip_sql_fences(raw_sql: str) -> str:
    """Remove blocos markdown ```sql ... ``` se o LLM tiver envolvido a query."""
    text = raw_sql.strip()
    match = _FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    return text


def _extract_cte_names(stmt: exp.Expression) -> set[str]:
    return {
        cte.alias.lower()
        for cte in stmt.find_all(exp.CTE)
        if cte.alias
    }


def _check_forbidden_functions(stmt: exp.Expression) -> None:
    for node in stmt.walk():
        if isinstance(node, exp.Anonymous):
            fn_name = (node.name or "").lower()
            if fn_name in FORBIDDEN_FUNCTIONS or fn_name.startswith("pragma_"):
                raise SQLValidationError(f"Função não permitida em consulta analítica: {fn_name}")
        elif isinstance(node, exp.Func):
            fn_name = node.sql_name().lower()
            if fn_name in FORBIDDEN_FUNCTIONS or fn_name.startswith("pragma_"):
                raise SQLValidationError(f"Função não permitida em consulta analítica: {fn_name}")


def _enforce_limit(stmt: exp.Query, default_limit: int, max_limit: int) -> exp.Query:
    limit_node = stmt.args.get("limit")
    if limit_node is None:
        return stmt.limit(default_limit)

    limit_expr = limit_node.expression
    if isinstance(limit_expr, exp.Literal) and limit_expr.is_int:
        current = int(limit_expr.this)
        if current <= 0:
            raise SQLValidationError("O valor de LIMIT deve ser maior que zero.")
        if current > max_limit:
            stmt.set("limit", exp.Limit(expression=exp.Literal.number(max_limit)))
        return stmt

    # Se o LIMIT for uma expressão não literal, substitui pelo teto seguro
    stmt.set("limit", exp.Limit(expression=exp.Literal.number(max_limit)))
    return stmt


def validate_and_prepare_sql(
    sql: str,
    allowed_tables: set[str] | None = None,
    default_limit: int = 100,
    max_limit: int = 500,
) -> str:
    """Valida o SQL via AST e retorna a query normalizada com LIMIT seguro.

    Raises:
        SQLValidationError: Se o SQL for inválido, destrutivo, multi-statement,
            usar funções proibidas ou referenciar tabelas fora da lista permitida.
    """
    cleaned = strip_sql_fences(sql)
    if not cleaned:
        raise SQLValidationError("Consulta SQL vazia.")

    try:
        statements = [s for s in sqlglot.parse(cleaned, read="duckdb") if s is not None]
    except ParseError as exc:
        raise SQLValidationError(f"Erro de sintaxe SQL: {exc}") from exc

    if len(statements) != 1:
        raise SQLValidationError(
            "Apenas uma única instrução SELECT é permitida por consulta (múltiplos statements bloqueados)."
        )

    stmt = statements[0]

    if not isinstance(stmt, (exp.Select, exp.Union)):
        raise SQLValidationError(
            f"Apenas consultas SELECT são permitidas (recebido: {stmt.__class__.__name__})."
        )

    for node in stmt.walk():
        if isinstance(node, FORBIDDEN_NODE_TYPES):
            raise SQLValidationError(
                f"Operação SQL não permitida: {node.__class__.__name__}."
            )

    _check_forbidden_functions(stmt)

    if allowed_tables is not None:
        allowed_lower = {t.lower() for t in allowed_tables}
        cte_names = _extract_cte_names(stmt)
        for table in stmt.find_all(exp.Table):
            table_name = (table.name or "").lower()
            if not table_name or table_name in cte_names:
                continue
            if table_name not in allowed_lower:
                raise SQLValidationError(
                    f"Tabela não autorizada na camada semântica: '{table.name}'."
                )

    stmt = _enforce_limit(stmt, default_limit=default_limit, max_limit=max_limit)
    return stmt.sql(dialect="duckdb", pretty=True)
