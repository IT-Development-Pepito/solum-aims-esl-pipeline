"""Runtime configuration for the ESL operations service."""

from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from explicit values or ``ESL_`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="ESL_", extra="forbid")

    environment: Literal["development", "staging", "production"]
    database_url: str
    internal_host: str
    shadow_mode: bool = True
    secret_bundle_path: Path = Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi")

    @model_validator(mode="after")
    def production_requires_internal_host(self) -> "Settings":
        if self.environment == "production" and not self.internal_host.strip():
            raise ValueError("internal_host must be configured for production")
        return self
