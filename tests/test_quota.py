"""Testes da cota permanente vinculada (anti-VPN e anti-aba anônima)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from analista_ia.config import PROJECT_ROOT, Settings
from analista_ia.web_server import create_app
from data.generate_data import generate_duckdb


@pytest.fixture()
def quota_app(tmp_path: Path):
    db_path = tmp_path / "seguros.duckdb"
    quota_db = tmp_path / "quota.sqlite3"
    generate_duckdb(db_path=db_path, n_clientes=120, n_apolices=240, seed=42)
    settings = Settings(
        llm_provider="mock",
        duckdb_path=db_path,
        quota_db_path=quota_db,
        redis_url="",
        upstash_redis_rest_url="",
        upstash_redis_rest_token="",
        quota_secret="test-secret-key-12345",
        lifetime_question_limit=4,
        rate_limit_per_minute_ip=20,
        rate_limit_max_requests=20,
        global_rpm_limit=50,
        daily_request_cap=100,
        semantic_layer_dir=PROJECT_ROOT / "semantic_layer",
    )
    return create_app(settings=settings, provider_override="mock")


def test_blocks_after_4_questions_even_when_switching_vpn_ip(quota_app) -> None:
    """Mesmo navegador (cookie/token) trocando de IP via VPN continua bloqueado em 4 perguntas."""
    client = TestClient(quota_app)
    token = "browser-device-token-abcdef123456"

    # Faz 2 perguntas no IP residencial
    for _ in range(2):
        r = client.post(
            "/api/ask",
            json={"question": "Qual a sinistralidade por ramo?"},
            headers={"X-Forwarded-For": "187.10.20.30", "X-Client-Token": token},
        )
        assert r.status_code == 200

    # Liga VPN (novo IP 45.99.88.77) e faz mais 2 perguntas -> chega a 4/4
    for i in range(2):
        r = client.post(
            "/api/ask",
            json={"question": "Qual o ticket médio por ramo?"},
            headers={"X-Forwarded-For": "45.99.88.77", "X-Client-Token": token},
        )
        assert r.status_code == 200
        assert r.json()["rate_limit"]["lifetime_remaining"] == 1 - i

    # Liga OUTRA VPN (terceiro IP 91.200.10.5) -> 5ª pergunta é bloqueada!
    r5 = client.post(
        "/api/ask",
        json={"question": "Qual a taxa de renovação por ramo?"},
        headers={"X-Forwarded-For": "91.200.10.5", "X-Client-Token": token},
    )
    assert r5.status_code == 429
    assert "4 perguntas" in r5.json()["detail"]


def test_blocks_incognito_tab_on_same_ip(quota_app) -> None:
    """Abrir aba anônima / limpar cookies no mesmo IP não reseta as 4 perguntas."""
    client_normal = TestClient(quota_app)
    ip = "200.150.100.50"

    for _ in range(4):
        r = client_normal.post(
            "/api/ask",
            json={"question": "Qual a sinistralidade por ramo?"},
            headers={"X-Forwarded-For": ip, "X-Client-Token": "token-aba-normal-111111"},
        )
        assert r.status_code == 200

    # Novo cliente sem cookies e com outro token (aba anônima), mas no mesmo IP
    client_incognito = TestClient(quota_app)
    r_incog = client_incognito.post(
        "/api/ask",
        json={"question": "Qual o ticket médio por ramo?"},
        headers={"X-Forwarded-For": ip, "X-Client-Token": "token-aba-anonima-999999"},
    )
    assert r_incog.status_code == 429
    assert "4 perguntas" in r_incog.json()["detail"]


class _FakeRedisPipeline:
    def __init__(self, store: dict[str, str]) -> None:
        self._store = store
        self._ops: list[tuple[str, str]] = []

    def set(self, k: str, v: str) -> None:
        self._ops.append((k, v))

    def execute(self) -> None:
        for k, v in self._ops:
            self._store[k] = v


class _FakeRedisClient:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def mget(self, keys: list[str]) -> list[str | None]:
        return [self.data.get(k) for k in keys]

    def pipeline(self) -> _FakeRedisPipeline:
        return _FakeRedisPipeline(self.data)


def test_standard_redis_quota_store_never_decreases(tmp_path: Path) -> None:
    from analista_ia.quota import SQLiteQuotaStore, StandardRedisQuotaStore

    fallback = SQLiteQuotaStore(tmp_path / "fb.sqlite3")
    fake_client = _FakeRedisClient()
    store = StandardRedisQuotaStore("redis://localhost:6379/0", fallback=fallback, client=fake_client)

    store.set_max_many(["k1", "k2"], 3)
    assert store.get_many(["k1", "k2"]) == {"k1": 3, "k2": 3}

    # Tentativa de gravar valor menor nunca reduz a cota já gasta
    store.set_max_many(["k1"], 1)
    assert store.get_many(["k1"]) == {"k1": 3}
