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
        # Preferisci alias ASCII su Render: all | next_days
        self.catalog_mode = os.getenv("CATALOG_MODE", "all").strip() or "all"
        self.max_workers = int(os.getenv("SISAL_MAX_WORKERS", "6"))
        # 1/true = anche mercati estesi (eventDetail, più lento).
        # 0/false = calcolo base (solo mercati da lista catalogo).
        self.include_extended = os.getenv(
            "SISAL_INCLUDE_EXTENDED", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        # Durata scansione pagata (ritocchi/riapertura gratis). Default 1 ora.
        self.cache_ttl_seconds = int(os.getenv("QUOTE_CACHE_TTL_SECONDS", "3600"))
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
            "STRIPE_SUCCESS_URL",
            "https://bonusguard-api.onrender.com/billing/success",
        ).strip()
        self.stripe_cancel_url = os.getenv(
            "STRIPE_CANCEL_URL",
            "https://bonusguard-api.onrender.com/billing/cancel",
        ).strip()

        # Produzione: default OFF. Locale: ALLOW_DEV_CREDIT_TOPUP=1 nel .env
        self.allow_dev_credit_topup = (
            os.getenv("ALLOW_DEV_CREDIT_TOPUP", "0").strip() == "1"
        )

        # CORS: lista separata da virgola, o * (solo se DEV)
        raw_origins = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
        if raw_origins:
            self.cors_allow_origins = [
                o.strip() for o in raw_origins.split(",") if o.strip()
            ]
        elif self.allow_dev_credit_topup:
            self.cors_allow_origins = ["*"]
        else:
            self.cors_allow_origins = [
                "https://bonusguard-api.onrender.com",
                "com.kiralab.bonusguard://",
            ]

        self.rate_limit_calc_per_min = int(
            os.getenv("RATE_LIMIT_CALC_PER_MIN", "6")
        )
        self.rate_limit_auth_per_min = int(
            os.getenv("RATE_LIMIT_AUTH_PER_MIN", "30")
        )

    @property
    def use_stub_engine(self) -> bool:
        return self.engine_mode in {"stub", "fake", "mock"}

    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key)

    @property
    def stripe_ready(self) -> bool:
        """Checkout utilizzabile in prod: secret + webhook secret."""
        return bool(self.stripe_secret_key and self.stripe_webhook_secret)
