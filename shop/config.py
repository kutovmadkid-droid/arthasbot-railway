import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    token: str
    admins: frozenset[int]
    db_path: str
    allow_manual_checkout: bool = False
    port: int = 8080

    @classmethod
    def from_env(cls):
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token or ":" not in token:
            raise ValueError("Set BOT_TOKEN to the token from BotFather")
        try:
            admins = frozenset(
                int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
            )
            if not admins or any(x <= 0 for x in admins):
                raise ValueError
        except ValueError:
            raise ValueError("Set ADMIN_IDS to comma-separated numeric Telegram user IDs") from None
        data_dir = os.environ.get("DATA_DIR", os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "./data"))
        on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_ENVIRONMENT_ID"))
        if on_railway:
            mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
            if not mount:
                raise ValueError("Railway requires a persistent volume mounted at /data before startup")
            if not Path(data_dir).resolve().is_relative_to(Path(mount).resolve()):
                raise ValueError("DATA_DIR must be inside the Railway persistent volume")
        try:
            port = int(os.environ.get("PORT", "8080"))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise ValueError("PORT must be an integer between 1 and 65535") from None
        return cls(
            token,
            admins,
            str(Path(data_dir) / "shop.sqlite3"),
            os.environ.get("ALLOW_MANUAL_CRYPTO_CHECKOUT", "false").lower() == "true",
            port,
        )
