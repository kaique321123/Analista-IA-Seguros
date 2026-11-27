"""Controle permanente de cota por visitante (anti-VPN e anti-aba anônima).

Combina 3 identificadores vinculados entre si (Cross-Linking Quota):
1. Hash HMAC-SHA256 do IP real (CF-Connecting-IP / X-Forwarded-For) — barra aba anônima.
2. Cookie HttpOnly assinado via HMAC-SHA256 (validade de 1 ano) — barra troca de IP / VPN.
3. Token persistente do navegador (header X-Client-Token / localStorage) — redundância no cliente.

Nenhum IP cru é salvo (apenas hashes com salt secreto, em conformidade com LGPD).
Suporta persistência em SQLite local (padrão) ou Upstash Redis REST (gratuito, ideal
para containers com disco efêmero como Render Free Tier).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Protocol

import httpx

from analista_ia.guardrails import RateLimitExceeded

COOKIE_NAME = "aid_visitor"
COOKIE_MAX_AGE = 365 * 24 * 3600  # 1 ano
QUOTA_KEY_PREFIX = "analista_ia:quota"
QUOTA_TTL_SECONDS = 90 * 24 * 3600  # 90 dias (evita acumular lixo em Redis compartilhado)


class QuotaStore(Protocol):
    """Interface de armazenamento chave-valor para contagem de perguntas."""

    def get_many(self, keys: list[str]) -> dict[str, int]: ...

    def set_max_many(self, keys: list[str], value: int) -> None: ...


class SQLiteQuotaStore:
    """Persistência leve em SQLite (thread-safe) para contadores de cota."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_counters (
                    key TEXT PRIMARY KEY,
                    used INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    def get_many(self, keys: list[str]) -> dict[str, int]:
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT key, used FROM quota_counters WHERE key IN ({placeholders})",
                keys,
            ).fetchall()
        return {k: int(v) for k, v in rows}

    def set_max_many(self, keys: list[str], value: int) -> None:
        if not keys:
            return
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO quota_counters (key, used)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET used = MAX(quota_counters.used, excluded.used)
                """,
                [(k, value) for k in keys],
            )
            conn.commit()


class StandardRedisQuotaStore:
    """Persistência via protocolo Redis padrão (redis:// ou rediss:// — Redis Cloud, Upstash, etc.)."""

    def __init__(self, redis_url: str, fallback: QuotaStore, client: object | None = None) -> None:
        import redis

        self.fallback = fallback
        self._client = client or redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=3.0,
            socket_timeout=3.0,
        )

    def get_many(self, keys: list[str]) -> dict[str, int]:
        if not keys:
            return {}
        try:
            values = self._client.mget(keys)  # type: ignore[attr-defined]
            return {
                k: int(v)
                for k, v in zip(keys, values)
                if v is not None and str(v).isdigit()
            }
        except Exception:
            return self.fallback.get_many(keys)

    def set_max_many(self, keys: list[str], value: int) -> None:
        self.fallback.set_max_many(keys, value)
        if not keys:
            return
        try:
            current = self.get_many(keys)
            pipe = self._client.pipeline()  # type: ignore[attr-defined]
            for k in keys:
                new_val = max(current.get(k, 0), value)
                pipe.set(k, str(new_val))
            pipe.execute()
        except Exception:
            pass


class UpstashRedisQuotaStore:
    """Persistência serverless gratuita via Upstash Redis REST API (sobrevive a deploys/sleep)."""

    def __init__(self, rest_url: str, rest_token: str, fallback: QuotaStore) -> None:
        self.rest_url = rest_url.rstrip("/")
        self.rest_token = rest_token
        self.fallback = fallback

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.rest_token}"}

    def get_many(self, keys: list[str]) -> dict[str, int]:
        if not keys:
            return {}
        try:
            resp = httpx.post(
                f"{self.rest_url}/pipeline",
                headers=self._headers(),
                json=[["MGET", *keys]],
                timeout=4.0,
            )
            resp.raise_for_status()
            values = resp.json()[0].get("result", [])
            return {
                k: int(v)
                for k, v in zip(keys, values)
                if v is not None and str(v).isdigit()
            }
        except Exception:
            return self.fallback.get_many(keys)

    def set_max_many(self, keys: list[str], value: int) -> None:
        self.fallback.set_max_many(keys, value)
        if not keys:
            return
        try:
            current = self.get_many(keys)
            cmds = [
                ["SET", k, str(max(current.get(k, 0), value)), "EX", str(QUOTA_TTL_SECONDS)]
                for k in keys
            ]
            httpx.post(
                f"{self.rest_url}/pipeline",
                headers=self._headers(),
                json=cmds,
                timeout=4.0,
            )
        except Exception:
            pass


class CrossLinkedQuotaManager:
    """Gerencia o limite fixo de N perguntas vinculando IP + Cookie HMAC + Client Token."""

    def __init__(
        self,
        limit: int,
        secret: str,
        store: QuotaStore,
    ) -> None:
        self.limit = limit
        self._secret_bytes = secret.encode("utf-8")
        self.store = store

    def _hash_id(self, prefix: str, raw_value: str) -> str:
        digest = hmac.new(
            self._secret_bytes,
            f"{prefix}:{raw_value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]
        return f"{QUOTA_KEY_PREFIX}:{prefix}:{digest}"

    def sign_visitor_id(self, visitor_id: str) -> str:
        sig = hmac.new(
            self._secret_bytes,
            visitor_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:24]
        return f"{visitor_id}.{sig}"

    def verify_or_create_visitor_cookie(self, cookie_value: str | None) -> tuple[str, str]:
        """Retorna (visitor_id, signed_cookie_value). Se inválido/ausente, cria novo."""
        if cookie_value and "." in cookie_value:
            visitor_id, sig = cookie_value.rsplit(".", 1)
            if 16 <= len(visitor_id) <= 64:
                expected = self.sign_visitor_id(visitor_id).rsplit(".", 1)[1]
                if hmac.compare_digest(sig, expected):
                    return visitor_id, cookie_value
        new_id = secrets.token_hex(16)
        return new_id, self.sign_visitor_id(new_id)

    def _build_keys(
        self,
        ip: str,
        visitor_id: str,
        client_token: str | None,
    ) -> list[str]:
        keys = [
            self._hash_id("ip", ip),
            self._hash_id("ck", visitor_id),
        ]
        if client_token:
            cleaned = client_token.strip()
            if 8 <= len(cleaned) <= 128:
                keys.append(self._hash_id("cl", cleaned))
        return keys

    def get_usage(
        self,
        ip: str,
        visitor_id: str,
        client_token: str | None = None,
    ) -> tuple[int, list[str]]:
        keys = self._build_keys(ip=ip, visitor_id=visitor_id, client_token=client_token)
        counts = self.store.get_many(keys)
        effective_used = max(counts.values(), default=0)
        # Se um identificador já tinha cota gasta (ex.: ligou VPN ou abriu aba anônima),
        # sincroniza imediatamente os outros identificadores para o mesmo patamar!
        if effective_used > 0 and len(counts) < len(keys):
            self.store.set_max_many(keys, effective_used)
        return effective_used, keys

    def status(
        self,
        ip: str,
        visitor_id: str,
        client_token: str | None = None,
    ) -> dict[str, int]:
        used, _ = self.get_usage(ip=ip, visitor_id=visitor_id, client_token=client_token)
        return {
            "lifetime_limit": self.limit,
            "lifetime_used": min(used, self.limit),
            "lifetime_remaining": max(self.limit - used, 0),
        }

    def check_and_consume(
        self,
        ip: str,
        visitor_id: str,
        client_token: str | None = None,
    ) -> dict[str, int]:
        used, keys = self.get_usage(ip=ip, visitor_id=visitor_id, client_token=client_token)
        if used >= self.limit:
            # Garante que o novo IP (VPN) ou novo cookie (aba anônima) também fique marcado no teto
            self.store.set_max_many(keys, used)
            raise RateLimitExceeded(
                f"Você atingiu o limite máximo de {self.limit} perguntas gratuitas desta demonstração. "
                "Para explorar sem limites, clone o repositório no GitHub e rode localmente!",
                retry_after=86400,
            )

        new_used = used + 1
        self.store.set_max_many(keys, new_used)
        return {
            "lifetime_limit": self.limit,
            "lifetime_used": new_used,
            "lifetime_remaining": max(self.limit - new_used, 0),
        }
