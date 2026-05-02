"""
credentials.py — FFL Pipeline 1 Credential Manager

Runtime secret loading is explicit and auditable.
Set FFL_SECRET_SOURCE to one of the following values:
- gcp_secret_manager
- env

No implicit fallback between secret sources is permitted.
"""
import logging
import os
from sqlalchemy import create_engine

logger = logging.getLogger(__name__)

DEFAULT_GCP_PROJECT_ID = "fractal-flow-art"
SECRET_SOURCE_ENV_VAR = "FFL_SECRET_SOURCE"
ALLOWED_SECRET_SOURCES = {"gcp_secret_manager", "env"}


# ── Secret retrieval ──────────────────────────────────────────────────────────

def _get_secret_source() -> str:
    """Return the explicitly configured runtime secret source."""
    source = os.environ.get(SECRET_SOURCE_ENV_VAR, "").strip().lower()
    if not source:
        raise RuntimeError(
            "FFL secret source is not configured. Set FFL_SECRET_SOURCE to "
            "'gcp_secret_manager' or 'env'. Implicit fallback is disabled."
        )
    if source not in ALLOWED_SECRET_SOURCES:
        raise RuntimeError(
            f"Invalid {SECRET_SOURCE_ENV_VAR} value '{source}'. "
            "Allowed values are 'gcp_secret_manager' and 'env'."
        )
    return source


def _get_secret_from_secret_manager(secret_id: str, project_id: str) -> str:
    """Retrieve a secret from Google Cloud Secret Manager."""
    try:
        from google.cloud import secretmanager
    except ImportError as exc:
        raise RuntimeError(
            "FFL secret source is set to 'gcp_secret_manager' but the "
            "google-cloud-secret-manager package is not installed."
        ) from exc

    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        value = response.payload.data.decode("UTF-8")
    except Exception as exc:
        raise RuntimeError(
            f"FFL secret source is set to 'gcp_secret_manager' but secret "
            f"access failed for '{secret_id}' in project '{project_id}'. "
            "No environment-variable fallback is permitted."
        ) from exc

    logger.info("Loaded secret '%s' via Google Secret Manager", secret_id)
    return value


def _get_secret_from_env(secret_id: str) -> str:
    """Retrieve a secret from an explicitly approved environment variable."""
    value = os.environ.get(secret_id)
    if not value:
        raise RuntimeError(
            f"FFL secret source is set to 'env' but environment variable "
            f"'{secret_id}' is missing."
        )

    logger.info("Loaded secret '%s' via explicit environment-variable mode", secret_id)
    return value


def _get_secret(secret_id: str, project_id: str | None = None) -> str:
    """Retrieve a secret using the explicitly configured source."""
    source = _get_secret_source()
    if source == "gcp_secret_manager":
        resolved_project_id = os.environ.get("FFL_GCP_PROJECT_ID", project_id or DEFAULT_GCP_PROJECT_ID)
        return _get_secret_from_secret_manager(secret_id, resolved_project_id)
    return _get_secret_from_env(secret_id)


# ── SQLAlchemy engine (shared across all agents) ──────────────────────────────

_engine = None


def get_sqlalchemy_engine():
    """
    Returns a shared SQLAlchemy engine connected to the FFL Neon database.
    Connection string is retrieved through the explicit FFL secret-loading mode.
    Engine is configured for serverless/short-lived workloads.
    """
    global _engine
    if _engine is not None:
        return _engine

    db_url = _get_secret("NEON_DB_URL")

    # pg8000 is a pure-Python PostgreSQL driver — no system libpq needed.
    # Strip all query params from URL and pass SSL via connect_args instead.
    import re
    import ssl

    pg8000_url = db_url.replace("postgresql://", "postgresql+pg8000://")
    # Remove all query string params (sslmode, channel_binding) — handled via connect_args
    pg8000_url = re.sub(r'\?.*$', '', pg8000_url)

    ssl_mode = os.environ.get("FFL_DB_SSL_MODE", "require").strip().lower()
    if ssl_mode not in {"require", "disable"}:
        raise RuntimeError("FFL_DB_SSL_MODE must be either 'require' or 'disable'")

    connect_args = {"timeout": 10}
    if ssl_mode == "require":
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        connect_args["ssl_context"] = ssl_ctx

    _engine = create_engine(
        pg8000_url,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=5,
        connect_args=connect_args,
    )
    logger.info("SQLAlchemy engine initialised (PostgreSQL, ssl_mode=%s)", ssl_mode)
    return _engine


# ── Webhook secrets ───────────────────────────────────────────────────────────

def get_shopify_webhook_secret() -> str:
    """Retrieve the Shopify webhook signing secret via the explicit secret source."""
    return _get_secret("SHOPIFY_WEBHOOK_SECRET")


def get_etsy_shared_secret() -> str:
    """Retrieve the Etsy shared secret via the explicit secret source."""
    return _get_secret("ETSY_SHARED_SECRET")
