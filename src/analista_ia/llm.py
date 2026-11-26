"""Abstração de provedores LLM configurável via variáveis de ambiente.

Usa httpx diretamente contra as APIs REST oficiais (Anthropic, OpenAI e Gemini)
para manter o projeto leve, desacoplado e fácil de trocar de provedor. Inclui um
provedor `mock` determinístico para testes automatizados e demonstrações offline.
"""

from __future__ import annotations

import unicodedata
from abc import ABC, abstractmethod

import httpx

from analista_ia.config import Settings


class LLMError(RuntimeError):
    """Erro na chamada ao provedor de LLM."""


class LLMClient(ABC):
    """Interface mínima para provedores de LLM."""

    provider_name: str = "base"

    @abstractmethod
    def complete(self, system_prompt: str, user_prompt: str, temperature: float = 0.0) -> str:
        """Gera uma resposta textual a partir de system + user prompt."""


class AnthropicClient(LLMClient):
    provider_name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-3-5-sonnet-latest") -> None:
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY não configurada no ambiente/.env.")
        self.api_key = api_key
        self.model = model or "claude-3-5-sonnet-latest"

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = 0.0) -> str:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 1200,
                "temperature": temperature,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=60.0,
        )
        if response.status_code >= 400:
            raise LLMError(f"Erro Anthropic ({response.status_code}): {response.text}")
        data = response.json()
        blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(blocks).strip()


class OpenAIClient(LLMClient):
    provider_name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o-mini") -> None:
        if not api_key:
            raise LLMError("OPENAI_API_KEY não configurada no ambiente/.env.")
        self.api_key = api_key
        self.model = model or "gpt-4o-mini"

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = 0.0) -> str:
        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=60.0,
        )
        if response.status_code >= 400:
            raise LLMError(f"Erro OpenAI ({response.status_code}): {response.text}")
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()


class GeminiClient(LLMClient):
    provider_name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash") -> None:
        if not api_key:
            raise LLMError("GEMINI_API_KEY não configurada no ambiente/.env.")
        self.api_key = api_key
        self.model = model or "gemini-2.5-flash"

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = 0.0) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        response = httpx.post(
            url,
            params={"key": self.api_key},
            headers={"Content-Type": "application/json"},
            json={
                "system_instruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": {"temperature": temperature},
            },
            timeout=60.0,
        )
        if response.status_code >= 400:
            raise LLMError(f"Erro Gemini ({response.status_code}): {response.text}")
        data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        return "\n".join(p.get("text", "") for p in parts).strip()


def _norm(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


class MockLLMClient(LLMClient):
    """Provedor determinístico para testes automatizados e uso offline sem chave de API."""

    provider_name = "mock"

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = 0.0) -> str:
        if "INTERPRETACAO_EXECUTIVA" in system_prompt:
            return (
                "**Resumo Executivo (modo mock):**\n"
                "- Os dados retornados pela consulta refletem as métricas oficiais da camada semântica.\n"
                "- Observe os segmentos ou ramos nos extremos da tabela para priorizar ações de precificação, "
                "retenção ou controle de sinistralidade.\n"
                "- Configure `LLM_PROVIDER` (`anthropic`, `openai` ou `gemini`) no `.env` para interpretações "
                "personalizadas via modelo de linguagem."
            )

        # Extrai apenas a pergunta de negócio para não confundir com palavras do contexto semântico
        marker = "=== PERGUNTA DE NEGÓCIO ==="
        if marker in user_prompt:
            question_part = user_prompt.split(marker, 1)[1].split("===", 1)[0]
        else:
            question_part = user_prompt
        q = _norm(question_part)

        if "sinistralidade" in q or "loss ratio" in q:
            return """
            WITH sinistros_por_apolice AS (
                SELECT apolice_id, SUM(valor_pago) AS total_pago
                FROM sinistros
                WHERE status_sinistro = 'Pago'
                GROUP BY apolice_id
            )
            SELECT
                a.ramo,
                ROUND(SUM(a.premio_emitido), 2) AS premio_emitido_total,
                ROUND(SUM(COALESCE(s.total_pago, 0)), 2) AS sinistros_pagos_total,
                ROUND(SUM(COALESCE(s.total_pago, 0)) * 100.0 / NULLIF(SUM(a.premio_emitido), 0), 2) AS sinistralidade_pct
            FROM apolices a
            LEFT JOIN sinistros_por_apolice s ON a.apolice_id = s.apolice_id
            GROUP BY a.ramo
            ORDER BY sinistralidade_pct DESC
            """

        if "churn" in q or "renovacao" in q or "retencao" in q:
            if "motivo" in q:
                return """
                SELECT
                    motivo_nao_renovacao,
                    COUNT(*) AS qtd_nao_renovadas
                FROM renovacoes
                WHERE elegivel_renovacao = TRUE AND renovada = FALSE
                GROUP BY motivo_nao_renovacao
                ORDER BY qtd_nao_renovadas DESC
                """
            if "canal" in q:
                return """
                SELECT
                    a.canal_venda,
                    COUNT(*) AS apolices_elegiveis,
                    ROUND(SUM(CASE WHEN r.renovada THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 2) AS taxa_renovacao_pct,
                    ROUND(SUM(CASE WHEN NOT r.renovada THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 2) AS taxa_churn_pct
                FROM renovacoes r
                JOIN apolices a ON r.apolice_id = a.apolice_id
                WHERE r.elegivel_renovacao = TRUE
                GROUP BY a.canal_venda
                ORDER BY taxa_renovacao_pct DESC
                """
            return """
            SELECT
                ramo,
                COUNT(*) AS apolices_elegiveis,
                ROUND(SUM(CASE WHEN renovada THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 2) AS taxa_renovacao_pct,
                ROUND(SUM(CASE WHEN NOT renovada THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 2) AS taxa_churn_pct
            FROM renovacoes
            WHERE elegivel_renovacao = TRUE
            GROUP BY ramo
            ORDER BY taxa_renovacao_pct DESC
            """

        if "ticket" in q:
            return """
            SELECT
                ramo,
                COUNT(DISTINCT apolice_id) AS quantidade_apolices,
                ROUND(AVG(premio_emitido), 2) AS ticket_medio
            FROM apolices
            GROUP BY ramo
            ORDER BY ticket_medio DESC
            """

        if "mensal" in q or "evolucao" in q or "mes" in q:
            return """
            SELECT
                STRFTIME(data_emissao, '%Y-%m') AS mes,
                COUNT(DISTINCT apolice_id) AS quantidade_apolices,
                ROUND(SUM(premio_emitido), 2) AS premio_emitido_total
            FROM apolices
            GROUP BY mes
            ORDER BY mes ASC
            """

        return """
        SELECT
            ramo,
            COUNT(DISTINCT apolice_id) AS quantidade_apolices,
            ROUND(SUM(premio_emitido), 2) AS premio_emitido_total,
            ROUND(AVG(premio_emitido), 2) AS ticket_medio
        FROM apolices
        GROUP BY ramo
        ORDER BY premio_emitido_total DESC
        """


def create_llm_client(settings: Settings, provider_override: str | None = None) -> LLMClient:
    """Instancia o cliente LLM conforme configuração ou override."""
    provider = (provider_override or settings.llm_provider).lower()
    if provider == "anthropic":
        return AnthropicClient(api_key=settings.anthropic_api_key, model=settings.llm_model)
    if provider == "openai":
        return OpenAIClient(api_key=settings.openai_api_key, model=settings.llm_model)
    if provider == "gemini":
        return GeminiClient(api_key=settings.gemini_api_key, model=settings.llm_model)
    if provider == "mock":
        return MockLLMClient()
    raise LLMError(f"Provedor LLM desconhecido: '{provider}'. Use anthropic, openai, gemini ou mock.")
