import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

LOCAL_ENV = "local"
PRODUCTION_ENV = "production"


def _normalize_app_env(raw_value: str) -> str:
    value = str(raw_value or "").strip().lower()
    if value in {"", "local", "development", "dev"}:
        return LOCAL_ENV
    if value in {"production", "prod"}:
        return PRODUCTION_ENV
    raise RuntimeError(
        "APP_ENV must be one of: local, production, development, dev, prod"
    )


APP_ENV = _normalize_app_env(os.getenv("APP_ENV", LOCAL_ENV))


@dataclass(frozen=True)
class MongoSettings:
    host: str
    port: str
    username: str
    password: str
    auth_source: str
    uri: str
    db_name: str


def is_production() -> bool:
    return APP_ENV == PRODUCTION_ENV


def get_mongo_settings() -> MongoSettings:
    if is_production():
        return MongoSettings(
            host=str(os.getenv("MONGODB_HOST", "")).strip(),
            port=str(os.getenv("MONGODB_PORT", "27017")).strip(),
            username=str(os.getenv("MONGODB_USERNAME", "")).strip(),
            password=str(os.getenv("MONGODB_PASSWORD", "")).strip(),
            auth_source=str(os.getenv("MONGODB_AUTH_SOURCE", "admin")).strip() or "admin",
            uri=str(os.getenv("MONGODB_URI", "")).strip(),
            db_name=str(os.getenv("MONGODB_DB_NAME", "")).strip(),
        )

    return MongoSettings(
        host=str(os.getenv("MONGODB_LOCAL_HOST", "localhost")).strip() or "localhost",
        port=str(os.getenv("MONGODB_LOCAL_PORT", "27017")).strip() or "27017",
        username=str(os.getenv("MONGODB_LOCAL_USERNAME", "")).strip(),
        password=str(os.getenv("MONGODB_LOCAL_PASSWORD", "")).strip(),
        auth_source=str(os.getenv("MONGODB_LOCAL_AUTH_SOURCE", "admin")).strip() or "admin",
        uri=str(os.getenv("MONGODB_LOCAL_URI", "")).strip(),
        db_name=str(os.getenv("MONGODB_LOCAL_DB_NAME", os.getenv("MONGODB_DB_NAME", "service_pro"))).strip() or "service_pro",
    )


def get_secret_key() -> str:
    return str(os.getenv("SECRET_KEY", "")).strip()


def get_notification_base_url() -> str:
    if is_production():
        return str(os.getenv("NOTIFICATION_BASE_URL", "")).strip().rstrip("/")

    return str(
        os.getenv("NOTIFICATION_LOCAL_BASE_URL", "http://127.0.0.1:5000")
    ).strip().rstrip("/")


def scheduler_enabled_flag() -> bool:
    enabled_flag = str(os.getenv("INVOICE_REMINDER_SCHEDULER_ENABLED", "true") or "").strip().lower()
    return enabled_flag not in {"0", "false", "no", "off"}


def scheduler_interval_minutes() -> int:
    raw_value = str(os.getenv("INVOICE_REMINDER_SCHEDULER_INTERVAL_MINUTES", "60") or "").strip()
    try:
        interval_minutes = int(raw_value)
    except (TypeError, ValueError):
        interval_minutes = 60
    return max(1, interval_minutes)


def validate_startup_config() -> None:
    if not get_secret_key():
        raise RuntimeError("SECRET_KEY environment variable is not set")

    mongo_settings = get_mongo_settings()
    if is_production():
        if not mongo_settings.db_name:
            raise RuntimeError("MONGODB_DB_NAME must be set when APP_ENV=production")
        if not mongo_settings.uri and not mongo_settings.host:
            raise RuntimeError(
                "Set MONGODB_URI or MONGODB_HOST when APP_ENV=production"
            )
