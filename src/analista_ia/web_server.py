"""Backend FastAPI com proteções de cota/rate-limit para a interface HTML/CSS."""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict, deque
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from analista_ia.agent import DataAnalystAgent
from analista_ia.config import PROJECT_ROOT, Settings, get_settings
from analista_ia.db import DuckDBExecutor
from analista_ia.semantic import load_semantic_layer

STATIC_DIR = PROJECT_ROOT / "web" / "static"


class RateLimiter:
    """Rate limiter em memória por IP (janela deslizante) + teto diário global."""

    def __init__(
        self,
        max_requests: int = 15,
        window_seconds: int = 600,
        daily_cap: int = 200,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.daily_cap = daily_cap
        self._ip_hits: dict[str, deque[float]] = defaultdict(deque)
        self._current_day: date = date.today()
        self._daily_count: int = 0

    def _reset_day_if_needed(self) -> None:
        today = date.today()
        if today != self._current_day:
            self._current_day = today
            self._daily_count = 0

    def _prune_ip(self, ip: str, now: float) -> deque[float]:
        q = self._ip_hits[ip]
        cutoff = now - self.window_seconds
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def status(self, ip: str) -> dict[str, int]:
        self._reset_day_if_needed()
        now = time.monotonic()
        q = self._prune_ip(ip, now)
        return {
            "max_requests": self.max_requests,
            "window_seconds": self.window_seconds,
            "remaining_ip": max(self.max_requests - len(q), 0),
            "daily_cap": self.daily_cap,
            "daily_remaining": max(self.daily_cap - self._daily_count, 0),
        }

    def check_and_consume(self, ip: str) -> dict[str, int]:
        self._reset_day_if_needed()
        if self._daily_count >= self.daily_cap:
            raise HTTPException(
                status_code=429,
                detail=(
                    "Limite diário da demonstração pública atingido. "
                    "Tente novamente amanhã ou rode localmente com sua própria chave."
                ),
            )

        now = time.monotonic()
        q = self._prune_ip(ip, now)
        if len(q) >= self.max_requests:
            retry_after = int(max(1, self.window_seconds - (now - q[0])))
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Limite de {self.max_requests} perguntas a cada "
                    f"{self.window_seconds // 60} min atingido para seu IP. "
                    f"Aguarde ~{retry_after}s para continuar."
                ),
                headers={"Retry-After": str(retry_after)},
            )

        q.append(now)
        self._daily_count += 1
        return self.status(ip)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=2000)


def _clean_json_value(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    if hasattr(val, "item"):
        return val.item()
    return val


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "local"


def _compute_portfolio_kpis(executor: DuckDBExecutor) -> dict[str, Any]:
    df_kpi = executor.execute_query(
        """
        WITH sin_agg AS (
            SELECT apolice_id, SUM(valor_pago) AS total_pago
            FROM sinistros
            WHERE status_sinistro = 'Pago'
            GROUP BY apolice_id
        ),
        apolice_kpi AS (
            SELECT
                COUNT(DISTINCT a.apolice_id) AS total_apolices,
                ROUND(SUM(a.premio_emitido), 2) AS premio_emitido_total,
                ROUND(AVG(a.premio_emitido), 2) AS ticket_medio,
                ROUND(
                    SUM(COALESCE(s.total_pago, 0)) * 100.0 / NULLIF(SUM(a.premio_emitido), 0),
                    2
                ) AS sinistralidade_pct
            FROM apolices a
            LEFT JOIN sin_agg s ON a.apolice_id = s.apolice_id
        ),
        ren_kpi AS (
            SELECT
                ROUND(
                    SUM(CASE WHEN renovada THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0),
                    2
                ) AS taxa_renovacao_pct
            FROM renovacoes
            WHERE elegivel_renovacao = TRUE
        )
        SELECT * FROM apolice_kpi, ren_kpi
        """
    )
    row = df_kpi.iloc[0].to_dict()
    return {k: _clean_json_value(v) for k, v in row.items()}


def create_app(
    settings: Settings | None = None,
    provider_override: str | None = None,
) -> FastAPI:
    cfg = settings or get_settings()
    semantic = load_semantic_layer(cfg.resolved_semantic_dir)
    executor = DuckDBExecutor(cfg.resolved_duckdb_path)
    limiter = RateLimiter(
        max_requests=cfg.rate_limit_max_requests,
        window_seconds=cfg.rate_limit_window_seconds,
        daily_cap=cfg.daily_request_cap,
    )

    app = FastAPI(
        title="Analista de Dados com IA — Seguros",
        version="0.1.0",
    )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/overview")
    def overview(request: Request) -> dict[str, Any]:
        ip = _client_ip(request)
        active_provider = provider_override or cfg.llm_provider
        return {
            "provider": active_provider,
            "model": cfg.llm_model or "(padrão)",
            "max_question_length": cfg.max_question_length,
            "kpis": _compute_portfolio_kpis(executor),
            "table_counts": executor.get_table_counts(),
            "rate_limit": limiter.status(ip),
            "semantic_layer": {
                "tables": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "grain": t.grain,
                        "columns": [
                            {
                                "name": c.name,
                                "type": c.type,
                                "description": c.description,
                                "values": c.values,
                            }
                            for c in t.columns
                        ],
                    }
                    for t in semantic.tables
                ],
                "metrics": [
                    {
                        "name": m.name,
                        "label": m.label,
                        "description": m.description.strip(),
                        "unit": m.unit,
                        "formula_sql": m.formula_sql.strip(),
                        "caveats": m.caveats.strip(),
                    }
                    for m in semantic.metrics
                ],
            },
        }

    @app.post("/api/ask")
    def ask(payload: AskRequest, request: Request) -> dict[str, Any]:
        question = payload.question.strip()
        if len(question) > cfg.max_question_length:
            raise HTTPException(
                status_code=422,
                detail=f"A pergunta excede o limite de {cfg.max_question_length} caracteres.",
            )

        ip = _client_ip(request)
        rl_status = limiter.check_and_consume(ip)

        try:
            agent = DataAnalystAgent.from_settings(
                settings=cfg,
                provider_override=provider_override,
            )
            result = agent.ask(question)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        df = result.dataframe
        rows = [
            {col: _clean_json_value(val) for col, val in record.items()}
            for record in df.to_dict(orient="records")
        ]
        chart_json = json.loads(result.chart.to_json()) if result.chart is not None else None

        return {
            "question": result.question,
            "matched_metrics": result.matched_metrics,
            "sql_validated": result.sql_validated,
            "interpretation": result.interpretation,
            "attempts": result.attempts,
            "provider": result.provider,
            "columns": list(df.columns),
            "rows": rows,
            "row_count": len(df),
            "chart": chart_json,
            "rate_limit": rl_status,
        }

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "analista_ia.web_server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
