from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    token: str
    prefix: str = "!"
    bot_name: str = "Banco Safra BOT"
    database_path: str = "data/banco_safra.db"
    log_path: str = "data/transactions.log"
    logo_path: str = "logo.png"
    low_balance_alert: float = 1000.0


def load_settings() -> Settings:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN nao foi definido. Configure a variavel no arquivo .env."
        )

    prefix = os.getenv("BOT_PREFIX", "!").strip() or "!"
    return Settings(token=token, prefix=prefix)
