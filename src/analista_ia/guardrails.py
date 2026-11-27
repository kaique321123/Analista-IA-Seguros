"""Guardrails de entrada e controle de taxa (RPM / janela / teto diário).

Bloqueia tentativas de prompt injection, injeção de SQL direto na pergunta,
pedidos que inflam o consumo de tokens e rajadas de requisições — tudo ANTES
de chamar a API do LLM (custo zero de tokens para entradas bloqueadas).
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import defaultdict, deque
from datetime import date


class GuardrailViolation(ValueError):
    """Erro levantado quando a entrada viola uma política de segurança ou custo."""

    def __init__(self, message: str, code: str = "guardrail_blocked") -> None:
        super().__init__(message)
        self.code = code


class RateLimitExceeded(Exception):
    """Erro levantado quando um limite de frequência (RPM, janela ou diário) é excedido."""

    def __init__(self, message: str, retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


# 1. Tentativas de Prompt Injection / vazamento de prompt
_PROMPT_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(ignore|desconsidere|esqueca)\b.{0,35}\b(instruc|regra|comando|sistema|anteriores|previous)\b"),
    re.compile(r"\b(system\s*prompt|prompt\s*do\s*sistema|regras\s*internas|instrucoes\s*internas)\b"),
    re.compile(r"\b(jailbreak|bypass|developer\s*mode|modo\s*desenvolvedor|dan\s*mode)\b"),
    re.compile(r"\b(finja\s*ser|atue\s*como\s*(um\s*)?(terminal|shell|banco|hacker|root))\b"),
    re.compile(r"\b(mostre|exiba|revele|imprima)\b.{0,30}\b(chave|api[\s_-]*key|token|secret|segredo|env|\.env)\b"),
]

# 2. Tentativas de forçar código SQL diretamente na pergunta do usuário
_RAW_SQL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"```"),
    re.compile(r";\s*(select|drop|delete|update|insert|alter|create|attach|copy|pragma|truncate)\b"),
    re.compile(r"\b(drop\s+table|delete\s+from|truncate\s+table|alter\s+table|insert\s+into|create\s+table)\b"),
    re.compile(r"\b(update\s+\w+\s+set|union\s+all?\s+select|select\s+.+\s+from\s+)\b"),
    re.compile(r"\b(read_csv|read_parquet|read_json|read_text|duckdb_|pragma_|information_schema|sqlite_master)"),
    re.compile(r"(--\s|/\*|\*/)"),
    re.compile(r"\b(execute\s+(o\s+)?(seguinte\s+)?sql|rode\s+(esta|essa|a)\s+query|roda\s+esse\s+select)\b"),
]

# 3. Pedidos desenhados para inflar tokens ou fora do escopo de BI conciso
_TOKEN_ABUSE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(escreva\s+um\s+(livro|artigo|ensaio|poema|codigo|script)|redacao\s+de)\b"),
    re.compile(r"\b(liste\s+todos\s+os\s+(clientes|registros|dados|apolices)|exporte\s+toda\s+a\s+tabela|dump\s+completo)\b"),
    re.compile(r"\b(repita\s+(a\s+palavra|isso)|gere\s+\d{3,}\s+(linhas|palavras|caracteres))\b"),
    re.compile(r"\b(crie\s+um\s+(script|programa|codigo)\s+(em\s+)?(python|javascript|bash|powershell))\b"),
]

# Spam de caracteres repetidos (ex.: "aaaaaaaaaa" ou "??????????")
_REPEATED_CHAR_RE = re.compile(r"(.)\1{9,}")


def validate_user_question(
    question: str,
    max_chars: int = 240,
    max_words: int = 45,
) -> str:
    """Valida e higieniza a pergunta do usuário antes de qualquer chamada ao LLM.

    Raises:
        GuardrailViolation: se a pergunta tentar prompt injection, SQL direto,
            abuso de tokens ou violar limites de tamanho.
    """
    clean = " ".join(question.strip().split())
    if len(clean) < 4:
        raise GuardrailViolation(
            "Faça uma pergunta de negócio completa em português (mínimo de 4 caracteres).",
            code="too_short",
        )

    if len(clean) > max_chars:
        raise GuardrailViolation(
            f"Pergunta muito longa ({len(clean)} caracteres). O limite é de {max_chars} caracteres "
            "para manter respostas objetivas e controlar o uso de tokens.",
            code="too_long",
        )

    words = clean.split()
    if len(words) > max_words:
        raise GuardrailViolation(
            f"Pergunta com muitas palavras ({len(words)}). Resuma em até {max_words} palavras.",
            code="too_many_words",
        )

    if _REPEATED_CHAR_RE.search(clean):
        raise GuardrailViolation(
            "Entrada bloqueada por padrão repetitivo/spam.",
            code="spam",
        )

    norm = _normalize(clean)

    for pattern in _PROMPT_INJECTION_PATTERNS:
        if pattern.search(norm):
            raise GuardrailViolation(
                "Pergunta bloqueada pelo guardrail de segurança (tentativa de alteração de instruções ou acesso interno).",
                code="prompt_injection",
            )

    for pattern in _RAW_SQL_PATTERNS:
        if pattern.search(norm):
            raise GuardrailViolation(
                "Não é permitido enviar código SQL diretamente. Faça sua pergunta em linguagem natural "
                "(ex.: 'Qual a sinistralidade por ramo?').",
                code="raw_sql_attempt",
            )

    for pattern in _TOKEN_ABUSE_PATTERNS:
        if pattern.search(norm):
            raise GuardrailViolation(
                "Pedido bloqueado pela política de controle de tokens. Faça perguntas analíticas "
                "agregadas sobre a carteira de seguros.",
                code="token_abuse",
            )

    return clean


class MultiTierRateLimiter:
    """Controla taxa em 3 camadas: RPM por IP, janela por IP, RPM global e teto diário."""

    def __init__(
        self,
        per_minute_ip: int = 4,
        max_requests_window_ip: int = 15,
        window_seconds: int = 600,
        global_rpm: int = 10,
        daily_cap: int = 150,
    ) -> None:
        self.per_minute_ip = per_minute_ip
        self.max_requests_window_ip = max_requests_window_ip
        self.window_seconds = window_seconds
        self.global_rpm = global_rpm
        self.daily_cap = daily_cap

        self._ip_minute_hits: dict[str, deque[float]] = defaultdict(deque)
        self._ip_window_hits: dict[str, deque[float]] = defaultdict(deque)
        self._global_minute_hits: deque[float] = deque()
        self._current_day: date = date.today()
        self._daily_count: int = 0

    def _reset_day_if_needed(self) -> None:
        today = date.today()
        if today != self._current_day:
            self._current_day = today
            self._daily_count = 0

    @staticmethod
    def _prune(q: deque[float], now: float, span: int) -> deque[float]:
        cutoff = now - span
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def status(self, ip: str) -> dict[str, int]:
        self._reset_day_if_needed()
        now = time.monotonic()
        ip_min = self._prune(self._ip_minute_hits[ip], now, 60)
        ip_win = self._prune(self._ip_window_hits[ip], now, self.window_seconds)
        glob_min = self._prune(self._global_minute_hits, now, 60)
        return {
            "per_minute_ip": self.per_minute_ip,
            "remaining_minute_ip": max(self.per_minute_ip - len(ip_min), 0),
            "max_requests": self.max_requests_window_ip,
            "window_seconds": self.window_seconds,
            "remaining_ip": max(self.max_requests_window_ip - len(ip_win), 0),
            "global_rpm": self.global_rpm,
            "remaining_global_rpm": max(self.global_rpm - len(glob_min), 0),
            "daily_cap": self.daily_cap,
            "daily_remaining": max(self.daily_cap - self._daily_count, 0),
        }

    def check_and_consume(self, ip: str) -> dict[str, int]:
        self._reset_day_if_needed()
        now = time.monotonic()

        if self._daily_count >= self.daily_cap:
            raise RateLimitExceeded(
                "Limite diário da demonstração pública atingido. "
                "Tente novamente amanhã ou rode localmente com sua própria chave.",
                retry_after=3600,
            )

        glob_min = self._prune(self._global_minute_hits, now, 60)
        if len(glob_min) >= self.global_rpm:
            retry = int(max(1, 60 - (now - glob_min[0])))
            raise RateLimitExceeded(
                f"Muitas solicitações simultâneas no servidor (limite global de {self.global_rpm}/min). "
                f"Aguarde ~{retry}s e tente novamente.",
                retry_after=retry,
            )

        ip_min = self._prune(self._ip_minute_hits[ip], now, 60)
        if len(ip_min) >= self.per_minute_ip:
            retry = int(max(1, 60 - (now - ip_min[0])))
            raise RateLimitExceeded(
                f"Limite de {self.per_minute_ip} perguntas por minuto atingido para seu IP. "
                f"Aguarde ~{retry}s.",
                retry_after=retry,
            )

        ip_win = self._prune(self._ip_window_hits[ip], now, self.window_seconds)
        if len(ip_win) >= self.max_requests_window_ip:
            retry = int(max(1, self.window_seconds - (now - ip_win[0])))
            raise RateLimitExceeded(
                f"Limite de {self.max_requests_window_ip} perguntas a cada "
                f"{self.window_seconds // 60} min atingido para seu IP. Aguarde ~{retry}s.",
                retry_after=retry,
            )

        glob_min.append(now)
        ip_min.append(now)
        ip_win.append(now)
        self._daily_count += 1
        return self.status(ip)
