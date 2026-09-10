import os
import pathlib
from dotenv import load_dotenv


# Load .env from the project root (two levels up from this file)
BASE_DIR = pathlib.Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


def _split_origins(value: str | None) -> list[str]:
    if not value:
        return []
    return [origin.strip() for origin in value.split(",") if origin.strip()]


# Environment
APP_ENV = os.getenv("APP_ENV", os.getenv("ENV", "development")).lower()

# Database
DATABASE_URL = os.getenv("DATABASE_URL")

# Auth
JWT_SECRET = os.getenv("JWT_SECRET")

# Frontend / CORS
FRONTEND_ORIGINS = _split_origins(os.getenv("FRONTEND_ORIGINS") or os.getenv("FRONTEND_URL"))
FRONTEND_ORIGIN_REGEX = os.getenv("FRONTEND_ORIGIN_REGEX")

# Bot / integrations
BOT_API_KEY = os.getenv("BOT_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Supabase storage
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = (
    os.getenv("SUPABASE_KEY")
    or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "uploads")


def validate_env(fail_on_warning: bool = True) -> None:
    missing = []
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if APP_ENV == "production":
        if not SUPABASE_URL:
            missing.append("SUPABASE_URL")

        if not SUPABASE_KEY:
            missing.append("SUPABASE_KEY")

        if not SUPABASE_BUCKET:
           missing.append("SUPABASE_BUCKET")
        if not JWT_SECRET or JWT_SECRET == "change-this-secret":
            missing.append("JWT_SECRET (set a strong secret in production)")
        # In production require explicit allowed origins
        if not FRONTEND_ORIGINS:
            missing.append("FRONTEND_ORIGINS (provide a comma-separated list of allowed origins)")
        # Recommend bot secrets in production if bot endpoints are used
        if not BOT_API_KEY:
            missing.append("BOT_API_KEY (required in production for bot endpoints)")
        if not BOT_TOKEN:
            missing.append("BOT_TOKEN (required in production for Telegram integration)")

    # Basic DB URL shape check (keep same policy: require postgresql)
    if DATABASE_URL and not DATABASE_URL.startswith("postgresql"):
        raise RuntimeError("DATABASE_URL must be a PostgreSQL URL in this deployment policy, e.g., postgresql://user:pass@host:port/dbname")

    if missing:
        message = "Required environment variables are missing or insecure:\n" + "\n".join([f" - {m}" for m in missing])
        if fail_on_warning:
            raise RuntimeError(message)
        else:
            print("WARNING:", message)


# Run validation on import to fail-fast when loaded in production
try:
    validate_env(fail_on_warning=(APP_ENV == "production"))
except Exception:
    # Re-raise so importers fail early in production
    raise
