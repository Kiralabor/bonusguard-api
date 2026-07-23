import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@lru_cache
def get_settings():
    return Settings()


class Settings:
    def __init__(self) -> None:
        # stub = risultati fake; live = fetch Sisal reale
        self.engine_mode = os.getenv("ENGINE_MODE", "live").strip().lower()
        self.catalog_mode = os.getenv(
            "CATALOG_MODE", "Tutte le partite (più lento)"
        ).strip()
        self.max_workers = int(os.getenv("SISAL_MAX_WORKERS", "6"))
        # 1/true = anche mercati estesi (eventDetail, più lento).
        # 0/false = calcolo base (solo mercati da lista catalogo).
        self.include_extended = os.getenv(
            "SISAL_INCLUDE_EXTENDED", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.cache_ttl_seconds = int(os.getenv("QUOTE_CACHE_TTL_SECONDS", "120"))
        self.sisal_http_proxy = (
            os.getenv("SISAL_HTTP_PROXY", "").strip()
            or os.getenv("SISAL_PROXY_URL", "").strip()
        )
        # Gateway IT (PC/casa): Render scarica le quote da qui invece che da sé.
        self.sisal_worker_url = os.getenv("SISAL_WORKER_URL", "").rstrip("/")
        self.sisal_worker_secret = os.getenv("SISAL_WORKER_SECRET", "").strip()

        self.supabase_url = os.getenv("SUPABASE_URL", "").strip()
        self.supabase_service_role_key = os.getenv(
            "SUPABASE_SERVICE_ROLE_KEY", ""
        ).strip()
        self.supabase_anon_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
        self.supabase_jwt_secret = os.getenv("SUPABASE_JWT_SECRET", "").strip()

        self.stripe_secret_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
        self.stripe_webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
        self.stripe_success_url = os.getenv(
            "STRIPE_SUCCESS_URL", "https://example.com/success"
        ).strip()
        self.stripe_cancel_url = os.getenv(
            "STRIPE_CANCEL_URL", "https://example.com/cancel"
        ).strip()

        self.allow_dev_credit_topup = os.getenv("ALLOW_DEV_CREDIT_TOPUP", "1") == "1"

    @property
    def use_stub_engine(self) -> bool:
        return self.engine_mode in {"stub", "fake", "mock"}

    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key)
