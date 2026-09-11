from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    rpc_url: str = "http://anvil:8545"
    db_url: str = "postgresql+psycopg://chainledger:chainledger@postgres:5432/chainledger"
    chain_id: int = 31337
    tokens: str = ""
    start_block: int = 0
    confirmations: int = Field(default=5, ge=0)
    poll_interval_seconds: float = Field(default=3.0, gt=0)
    batch_size: int = Field(default=500, ge=1, le=10000)
    metrics_port: int = Field(default=8001, ge=1, le=65535)

    @property
    def token_addresses(self) -> list[str]:
        return [t.strip().lower() for t in self.tokens.split(",") if t.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
