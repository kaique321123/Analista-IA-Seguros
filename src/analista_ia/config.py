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

    llm_provider: LLMProviderName = Field(default="anthropic")
    llm_model: str = Field(default="")

    anthropic_api_key: str = Field(default="")
    openai_api_key: str = Field(default="")
    gemini_api_key: str = Field(default="")

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
