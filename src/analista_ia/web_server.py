"""Backend FastAPI com proteções de cota/rate-limit para a interface HTML/CSS."""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from analista_ia.agent import DataAnalystAgent
from analista_ia.config import PROJECT_ROOT, Settings, get_settings
from analista_ia.db import DuckDBExecutor
from analista_ia.guardrails import (
    GuardrailViolation,
    MultiTierRateLimiter,
    RateLimitExceeded,
    validate_user_question,
)
from analista_ia.quota import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    CrossLinkedQuotaManager,
    QuotaStore,
    SQLiteQuotaStore,
    UpstashRedisQuotaStore,
)
from analista_ia.semantic import load_semantic_layer

STATIC_DIR = PROJECT_ROOT / "web" / "static"


class NoCacheStaticFiles(StaticFiles):
    """Serve os arquivos estáticos sem cache para que ajustes de design apareçam ao recarregar."""

    async def get_response(self, path, scope):  # type: ignore[no-untyped-def]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)


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
    for header in ("cf-connecting-ip", "true-client-ip", "x-real-ip"):
        val = request.headers.get(header)
        if val and val.strip():
            return val.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "local"


def _set_visitor_cookie(response: Response, signed_value: str, request: Request) -> None:
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        key=COOKIE_NAME,
        value=signed_value,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )


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
    limiter = MultiTierRateLimiter(
        per_minute_ip=cfg.rate_limit_per_minute_ip,
        max_requests_window_ip=cfg.rate_limit_max_requests,
        window_seconds=cfg.rate_limit_window_seconds,
        global_rpm=cfg.global_rpm_limit,
        daily_cap=cfg.daily_request_cap,
    )
    sqlite_store: QuotaStore = SQLiteQuotaStore(cfg.resolved_quota_db_path)
    quota_store: QuotaStore = (
        UpstashRedisQuotaStore(
            rest_url=cfg.upstash_redis_rest_url,
            rest_token=cfg.upstash_redis_rest_token,
            fallback=sqlite_store,
        )
        if cfg.upstash_redis_rest_url and cfg.upstash_redis_rest_token
        else sqlite_store
    )
    quota_manager = CrossLinkedQuotaManager(
        limit=cfg.lifetime_question_limit,
        secret=cfg.quota_secret,
        store=quota_store,
    )

    app = FastAPI(
        title="Analista de Dados com IA — Seguros",
        version="0.1.0",
    )

    if STATIC_DIR.exists():
        app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @app.get("/api/overview")
    def overview(request: Request, response: Response) -> dict[str, Any]:
        ip = _client_ip(request)
        visitor_id, signed_cookie = quota_manager.verify_or_create_visitor_cookie(
            request.cookies.get(COOKIE_NAME)
        )
        _set_visitor_cookie(response, signed_cookie, request)
        client_token = request.headers.get("x-client-token")

        rl = limiter.status(ip)
        q_status = quota_manager.status(ip=ip, visitor_id=visitor_id, client_token=client_token)
        merged_rl = {
            **rl,
            **q_status,
            "max_requests": q_status["lifetime_limit"],
            "remaining_ip": min(rl["remaining_ip"], q_status["lifetime_remaining"]),
        }

        active_provider = provider_override or cfg.llm_provider
        return {
            "provider": active_provider,
            "model": cfg.llm_model or "(padrão)",
            "max_question_length": cfg.max_question_length,
            "kpis": _compute_portfolio_kpis(executor),
            "table_counts": executor.get_table_counts(),
            "rate_limit": merged_rl,
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
    def ask(payload: AskRequest, request: Request, response: Response) -> dict[str, Any]:
        visitor_id, signed_cookie = quota_manager.verify_or_create_visitor_cookie(
            request.cookies.get(COOKIE_NAME)
        )
        _set_visitor_cookie(response, signed_cookie, request)
        client_token = request.headers.get("x-client-token")

        # 1. Guardrail de entrada ANTES da cota e do LLM (custo zero de tokens)
        try:
            question = validate_user_question(
                payload.question,
                max_chars=cfg.max_question_length,
                max_words=cfg.max_question_words,
            )
        except GuardrailViolation as exc:
            status_code = 422 if exc.code in {"too_long", "too_short", "too_many_words"} else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

        # 2. Cota permanente vinculada (IP + Cookie assinado + Client Token) + Rate limit RPM/diário
        ip = _client_ip(request)
        try:
            # Verifica primeiro se ainda possui saldo nas 4 perguntas totais antes de consumir RPM
            current_used, _ = quota_manager.get_usage(
                ip=ip, visitor_id=visitor_id, client_token=client_token
            )
            if current_used >= quota_manager.limit:
                quota_manager.check_and_consume(
                    ip=ip, visitor_id=visitor_id, client_token=client_token
                )
            rl = limiter.check_and_consume(ip)
            q_status = quota_manager.check_and_consume(
                ip=ip, visitor_id=visitor_id, client_token=client_token
            )
            rl_status = {
                **rl,
                **q_status,
                "max_requests": q_status["lifetime_limit"],
                "remaining_ip": min(rl["remaining_ip"], q_status["lifetime_remaining"]),
            }
        except RateLimitExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc

        # 3. Execução do agente Text-to-SQL
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
    import os
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    print(f"Analista de Dados com IA disponível em: http://localhost:{port}")
    uvicorn.run(
        "analista_ia.web_server:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
