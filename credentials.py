"""
credentials.py — FFL Pipeline 1 Credential Manager
Retrieves NEON_DB_URL from Google Cloud Secret Manager at runtime.
Falls back to environment variable for local development.
"""
import os
import logging
from sqlalchemy import create_engine

logger = logging.getLogger(__name__)

# ── Secret retrieval ──────────────────────────────────────────────────────────

def _get_secret(secret_id: str, project_id: str = "fractal-flow-art") -> str:
    """Retrieve a secret from Google Cloud Secret Manager."""
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logger.warning(f"Secret Manager unavailable ({e}), falling back to env var {secret_id}")
        value = os.environ.get(secret_id)
        if not value:
            raise RuntimeError(
                f"Secret '{secret_id}' not found in Secret Manager or environment variables."
            )
        return value


# ── SQLAlchemy engine (shared across all agents) ──────────────────────────────

_engine = None

def get_sqlalchemy_engine():
    """
    Returns a shared SQLAlchemy engine connected to the FFL Neon database.
    Connection string is retrieved from Google Cloud Secret Manager (NEON_DB_URL).
    Engine is configured for serverless/short-lived workloads.
    """
    global _engine
    if _engine is not None:
        return _engine

    db_url = _get_secret("NEON_DB_URL")

    # pg8000 is a pure-Python PostgreSQL driver — no system libpq needed.
    # Strip all query params from URL and pass SSL via connect_args instead.
    import ssl
    import re
    pg8000_url = db_url.replace("postgresql://", "postgresql+pg8000://")
    # Remove all query string params (sslmode, channel_binding) — handled via connect_args
    pg8000_url = re.sub(r'\?.*$', '', pg8000_url)

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    _engine = create_engine(
        pg8000_url,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=5,
        connect_args={
            "ssl_context": ssl_ctx,
            "timeout": 10,
        },
    )
    logger.info("SQLAlchemy engine initialised (Neon PostgreSQL)")
    return _engine


# ── Webhook secrets ───────────────────────────────────────────────────────────

def get_shopify_webhook_secret() -> str:
    """Retrieve the Shopify webhook signing secret from Secret Manager."""
    return _get_secret("SHOPIFY_WEBHOOK_SECRET")


def get_etsy_shared_secret() -> str:
    """Retrieve the Etsy shared secret (used for webhook verification) from Secret Manager."""
    return _get_secret("ETSY_SHARED_SECRET")
