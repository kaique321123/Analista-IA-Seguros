"""Testes da API FastAPI e do rate limiter da interface web."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from analista_ia.config import PROJECT_ROOT, Settings
from analista_ia.web_server import create_app
from data.generate_data import generate_duckdb


@pytest.fixture(scope="module")
def web_client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    db_path = tmp_path_factory.mktemp("web_db") / "seguros.duckdb"
    generate_duckdb(db_path=db_path, n_clientes=200, n_apolices=400, seed=42)
    settings = Settings(
        llm_provider="mock",
        duckdb_path=db_path,
        semantic_layer_dir=PROJECT_ROOT / "semantic_layer",
        rate_limit_max_requests=2,
        rate_limit_window_seconds=600,
        daily_request_cap=10,
        max_question_length=120,
    )
    app = create_app(settings=settings, provider_override="mock")
    return TestClient(app)


def test_overview_returns_kpis_and_semantic_layer(web_client: TestClient) -> None:
    res = web_client.get("/api/overview")
    assert res.status_code == 200
    data = res.json()
    assert data["kpis"]["total_apolices"] == 400
    assert len(data["semantic_layer"]["metrics"]) >= 8
    assert data["rate_limit"]["remaining_ip"] == 2


def test_ask_endpoint_and_rate_limit_protection(web_client: TestClient) -> None:
    # 1ª pergunta: OK
    r1 = web_client.post("/api/ask", json={"question": "Qual a sinistralidade por ramo?"})
    assert r1.status_code == 200
    body = r1.json()
    assert body["row_count"] > 0
    assert body["chart"] is not None
    assert body["rate_limit"]["remaining_ip"] == 1

    # 2ª pergunta: OK
    r2 = web_client.post("/api/ask", json={"question": "Qual o ticket médio por ramo?"})
    assert r2.status_code == 200
    assert r2.json()["rate_limit"]["remaining_ip"] == 0

    # 3ª pergunta dentro da janela: bloqueada com HTTP 429
    r3 = web_client.post("/api/ask", json={"question": "Qual a taxa de renovação por ramo?"})
    assert r3.status_code == 429


def test_rejects_oversized_question(web_client: TestClient) -> None:
    r = web_client.post("/api/ask", json={"question": "a" * 150})
    assert r.status_code == 422
