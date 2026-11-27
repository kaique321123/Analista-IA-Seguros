"""Configurações centralizadas da aplicação via variáveis de ambiente."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

LLMProviderName = Literal["anthropic", "openai", "gemini", "mock"]


class Settings(BaseSettings):
    """Configurações carregadas de variáveis de ambiente e arquivo .env."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: LLMProviderName = Field(default="gemini")
    llm_model: str = Field(default="")
    llm_max_output_tokens: int = Field(default=600, ge=100, le=4096)
    llm_timeout_seconds: float = Field(default=45.0, ge=5.0, le=180.0)

    # Proteções da API web e guardrails para demonstrações públicas temporárias
    rate_limit_per_minute_ip: int = Field(default=4, ge=1, le=120)
    rate_limit_max_requests: int = Field(default=15, ge=1, le=1000)
    rate_limit_window_seconds: int = Field(default=600, ge=10, le=86400)
    global_rpm_limit: int = Field(default=10, ge=1, le=500)
    daily_request_cap: int = Field(default=150, ge=1, le=50000)
    max_question_length: int = Field(default=240, ge=20, le=2000)
    max_question_words: int = Field(default=45, ge=5, le=400)

    gemini_api_key: str = Field(default="")
    anthropic_api_key: str = Field(default="")
    openai_api_key: str = Field(default="")

    duckdb_path: Path = Field(default=PROJECT_ROOT / "data" / "seguros.duckdb")
    semantic_layer_dir: Path = Field(default=PROJECT_ROOT / "semantic_layer")

    sql_default_limit: int = Field(default=100, ge=1, le=5000)
    sql_max_limit: int = Field(default=500, ge=1, le=10000)

    def resolve_path(self, path: Path) -> Path:
        """Resolve caminhos relativos a partir da raiz do projeto."""
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    @property
    def resolved_duckdb_path(self) -> Path:
        return self.resolve_path(self.duckdb_path)

    @property
    def resolved_semantic_dir(self) -> Path:
        return self.resolve_path(self.semantic_layer_dir)


def get_settings() -> Settings:
    """Retorna uma instância de configurações atualizada."""
    return Settings()
