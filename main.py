"""
FFL Pipeline 1 — Webhook Receiver + Agent Data Writer
Accepts order webhooks from Shopify and Etsy.
Writes directly to Neon PostgreSQL (orders + inventory_logs tables).
Triggers Printful fulfillment AFTER successful Neon write.
Also provides internal endpoints for agent swarm to write trend/research data to Neon.

Endpoints:
  POST /webhooks/shopify/orders/create   — Shopify new order
  POST /webhooks/etsy/orders/create      — Etsy new order (receipt)
  GET  /health                           — Health check
  POST /internal/hook_library            — Agent 2: write trend row to agent2_trend_research table
  POST /internal/trend_shift             — Agent 7: write Vibe-Shift Brief to agent7_vibe_shift_briefs table
"""

import hashlib
import hmac
import base64
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from credentials import get_sqlalchemy_engine, get_shopify_webhook_secret, get_etsy_shared_secret
from taske_runtime_controls import (
    router as taske_router,
    build_shopify_order_record,
    build_etsy_order_record,
    create_finance_locked_order,
    write_inventory_log_record,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ffl.pipeline1")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="FFL Pipeline 1 — Webhook Receiver",
    description="Shopify & Etsy order webhooks → Neon PostgreSQL",
    version="1.1.0",
)
app.include_router(taske_router)


# ── Legal Pages ──────────────────────────────────────────────────────────────

@app.get("/terms", response_class=HTMLResponse)
async def terms_of_service():
    """Terms of Service for FFL Swarm Integrator."""
    return """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Terms of Service — FFL Swarm Integrator</title>
<style>body{font-family:sans-serif;max-width:800px;margin:40px auto;padding:0 20px;line-height:1.6;color:#222}h1{font-size:1.6em}h2{font-size:1.2em;margin-top:2em}p{margin:0.8em 0}</style>
</head>
<body>
<h1>Terms of Service — FFL Swarm Integrator</h1>
<p><strong>Last updated: 12 April 2026</strong></p>
<p>FFL Swarm Integrator ("the App") is a private automation tool operated by Fractal Flow Lab (ABN pending), an individual creator business based in Australia. The App integrates with TikTok's developer platform to schedule and publish content and retrieve analytics for the <strong>fractalflowlab_</strong> TikTok account.</p>
<h2>1. Scope of Use</h2>
<p>The App is used exclusively by the account owner of Fractal Flow Lab. It is not a public-facing application and does not provide services to third-party users. No other individuals or organisations are authorised to use the App.</p>
<h2>2. TikTok API Usage</h2>
<p>The App uses TikTok's Content Posting API and Display API solely to publish fractal art content and retrieve performance metrics for the fractalflowlab_ account. All API usage complies with TikTok's Platform Terms of Service and Developer Policies.</p>
<h2>3. Data Handling</h2>
<p>All data retrieved from TikTok (including post metrics and account information) is stored privately in a secured database operated by Fractal Flow Lab. No TikTok user data is shared with, sold to, or accessible by any third party.</p>
<h2>4. Limitation of Liability</h2>
<p>The App is provided as-is for internal operational use. Fractal Flow Lab accepts no liability for service interruptions, data loss, or API changes made by TikTok.</p>
<h2>5. Contact</h2>
<p>For any questions regarding these terms, contact: <a href="mailto:Fractalflowart@gmail.com">Fractalflowart@gmail.com</a></p>
</body></html>
"""


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    """Privacy Policy for FFL Swarm Integrator."""
    return """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Privacy Policy — FFL Swarm Integrator</title>
<style>body{font-family:sans-serif;max-width:800px;margin:40px auto;padding:0 20px;line-height:1.6;color:#222}h1{font-size:1.6em}h2{font-size:1.2em;margin-top:2em}p{margin:0.8em 0}</style>
</head>
<body>
<h1>Privacy Policy — FFL Swarm Integrator</h1>
<p><strong>Last updated: 12 April 2026</strong></p>
<p>This Privacy Policy describes how Fractal Flow Lab ("we", "us") collects, uses, and protects information in connection with the FFL Swarm Integrator application.</p>
<h2>1. Information We Collect</h2>
<p>The App accesses the following data from TikTok via the official TikTok API, solely for the fractalflowlab_ account:</p>
<ul>
<li>Basic account information (username, follower count, profile details)</li>
<li>Post performance metrics (views, likes, shares, comments, engagement rate)</li>
<li>Content upload and scheduling data</li>
</ul>
<h2>2. How We Use Information</h2>
<p>All data collected is used exclusively for internal analytics and content scheduling by the account owner. We do not use TikTok data for advertising, profiling, or any commercial purpose beyond managing the fractalflowlab_ account.</p>
<h2>3. Data Storage</h2>
<p>Performance metrics are stored in a private, encrypted PostgreSQL database (Neon.tech, hosted on AWS ap-southeast-2, Sydney, Australia). No TikTok user data from third parties is stored.</p>
<h2>4. Data Sharing</h2>
<p>We do not sell, share, rent, or disclose any data to third parties. The App is strictly private and single-user.</p>
<h2>5. Data Retention</h2>
<p>Analytics data is retained for up to 24 months for business performance tracking and then deleted.</p>
<h2>6. Your Rights</h2>
<p>As this App is single-user and operated by the account owner, all data is under the direct control of the operator. For any privacy enquiries, contact: <a href="mailto:Fractalflowart@gmail.com">Fractalflowart@gmail.com</a></p>
<h2>7. Changes to This Policy</h2>
<p>We may update this Privacy Policy from time to time. The latest version will always be available at this URL.</p>
</body></html>
"""


# ── TikTok OAuth Callback ───────────────────────────────────────────────────

@app.get("/auth/tiktok/callback", response_class=HTMLResponse)
async def tiktok_oauth_callback(request: Request):
    """
    TikTok OAuth 2.0 callback endpoint.
    Receives the authorization code after the user authorises the app.
    Displays the code for manual token exchange during initial setup.
    """
    code = request.query_params.get("code")
    error = request.query_params.get("error")
    state = request.query_params.get("state", "")

    if error:
        logger.warning(f"TikTok OAuth error: {error}")
        return f"<h2>TikTok Auth Error</h2><p>{error}</p>"

    if code:
        logger.info(f"TikTok OAuth code received (state={state})")
        return f"""
        <!DOCTYPE html><html><head><title>TikTok Auth</title>
        <style>body{{font-family:sans-serif;max-width:600px;margin:60px auto;padding:0 20px}}
        code{{background:#f4f4f4;padding:8px 12px;display:block;word-break:break-all;border-radius:4px}}</style>
        </head><body>
        <h2>TikTok Authorisation Successful</h2>
        <p>Copy the authorisation code below and provide it to the FFL agent swarm to complete token exchange:</p>
        <code>{code}</code>
        <p style="color:#888;font-size:0.9em">This code expires in 10 minutes. Do not share it.</p>
        </body></html>
        """

    return "<h2>TikTok Auth</h2><p>No code received.</p>"


# ── TikTok Webhook Handler ───────────────────────────────────────────────────

@app.post("/webhooks/tiktok")
async def tiktok_webhook(request: Request):
    """
    Handles TikTok webhook events (video status updates, comments, etc.).
    Returns HTTP 200 immediately to acknowledge receipt.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    event_type = payload.get("event", "unknown")
    logger.info(f"TikTok webhook received — event: {event_type}")
    return JSONResponse(status_code=200, content={"status": "received", "event": event_type})


# ── TikTok Site Verification ─────────────────────────────────────────────────

@app.get("/tiktokCdYKnoArSY5rB4oLeckQEFPOfEn8Zc6L.txt", response_class=PlainTextResponse)
async def tiktok_verification():
    """TikTok developer site verification file."""
    return "tiktok-developers-site-verification=CdYKnoArSY5rB4oLeckQEFPOfEn8Zc6L"


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Returns 200 OK with database connectivity status."""
    try:
        engine = get_sqlalchemy_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": "unreachable", "detail": str(e)},
        )


# ── HMAC Verification ─────────────────────────────────────────────────────────

def _verify_shopify_hmac(raw_body: bytes, hmac_header: str) -> bool:
    """
    Verify Shopify webhook HMAC-SHA256 signature.
    Shopify sends: X-Shopify-Hmac-Sha256: base64(HMAC-SHA256(body, secret))
    """
    try:
        secret = get_shopify_webhook_secret().encode("utf-8")
        digest = hmac.new(secret, raw_body, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(expected, hmac_header)
    except Exception as e:
        logger.error(f"Shopify HMAC verification error: {e}")
        return False


def _verify_etsy_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Verify Etsy webhook signature.
    Etsy sends: X-Etsy-Signature: HMAC-SHA256(body, shared_secret) as hex digest
    """
    try:
        secret = get_etsy_shared_secret().encode("utf-8")
        digest = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, signature_header.lower())
    except Exception as e:
        logger.error(f"Etsy signature verification error: {e}")
        return False


# ── Neon Write Helpers ────────────────────────────────────────────────────────

def _write_order_to_neon(order_record) -> int:
    """
    INSERT a finance-locked order into the Neon orders table.
    Returns the new order_id (SERIAL primary key).
    Raises on duplicate external_order_id (UNIQUE constraint).
    """
    return create_finance_locked_order(order_record)


def _write_inventory_log(
    order_id: int,
    product_id: int | None,
    printful_order_id: str | None,
    fulfillment_status: str,
) -> None:
    """INSERT a row into inventory_logs after fulfillment confirmation."""
    write_inventory_log_record(
        order_id=order_id,
        product_id=product_id,
        printful_order_id=printful_order_id,
        fulfillment_status=fulfillment_status,
    )


# ── Shopify Webhook Handler ───────────────────────────────────────────────────

@app.post("/webhooks/shopify/orders/create")
async def shopify_order_created(
    request: Request,
    x_shopify_hmac_sha256: str = Header(None),
    x_shopify_topic: str = Header(None),
    x_shopify_shop_domain: str = Header(None),
):
    """
    Handles Shopify orders/create webhook.
    Verifies HMAC, writes to Neon orders table, returns 200 immediately.
    Printful fulfillment is triggered asynchronously by Agent 1.
    """
    raw_body = await request.body()

    # 1. Verify HMAC
    if not x_shopify_hmac_sha256:
        logger.warning("Shopify webhook received without HMAC header — rejected")
        raise HTTPException(status_code=401, detail="Missing HMAC header")

    if not _verify_shopify_hmac(raw_body, x_shopify_hmac_sha256):
        logger.warning("Shopify HMAC verification FAILED — possible spoofed request")
        raise HTTPException(status_code=401, detail="HMAC verification failed")

    # 2. Parse payload
    payload: Dict[str, Any] = await request.json() if not raw_body else __import__("json").loads(raw_body)

    try:
        order_record = build_shopify_order_record(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    external_order_id = order_record.external_order_id

    # 3. Write to Neon
    try:
        order_id = _write_order_to_neon(order_record)
    except Exception as e:
        error_msg = str(e)
        if "unique" in error_msg.lower() or "duplicate" in error_msg.lower():
            logger.info(f"Duplicate Shopify order {external_order_id} — already in Neon, skipping")
            return {"status": "duplicate", "message": "Order already recorded"}
        logger.error(f"Neon write failed for Shopify order {external_order_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Database write failed: {e}")

    # 4. Log initial inventory entry (Printful fulfillment pending)
    try:
        _write_inventory_log(
            order_id=order_id,
            product_id=None,  # Resolved by Agent 1 via SKU lookup
            printful_order_id=None,  # Set after Printful confirms
            fulfillment_status="pending_fulfillment",
        )
    except Exception as e:
        logger.warning(f"inventory_logs write failed (non-fatal): {e}")

    logger.info(
        "Shopify order %s processed — Neon order_id=%s, %s %s locked to AUD %s, shop=%s",
        external_order_id,
        order_id,
        order_record.currency_code,
        order_record.gross_amount_foreign,
        order_record.gross_amount_aud_locked,
        x_shopify_shop_domain,
    )

    return {
        "status": "ok",
        "platform": "Shopify",
        "external_order_id": external_order_id,
        "neon_order_id": order_id,
        "currency_code": order_record.currency_code,
        "gross_amount_aud_locked": str(order_record.gross_amount_aud_locked),
        "gst_reserve_aud": str(order_record.gst_reserve_aud),
    }


# ── Etsy Webhook Handler ──────────────────────────────────────────────────────

@app.post("/webhooks/etsy/orders/create")
async def etsy_order_created(
    request: Request,
    x_etsy_signature: str = Header(None),
):
    """
    Handles Etsy receipt.created webhook (new order).
    Verifies signature, writes to Neon orders table.

    Note: Etsy calls orders "receipts". The receipt_id is used as external_order_id.
    Etsy webhooks are only available once your developer app is approved.
    """
    raw_body = await request.body()

    # 1. Verify signature (skip in dev mode if secret not yet configured)
    if x_etsy_signature:
        if not _verify_etsy_signature(raw_body, x_etsy_signature):
            logger.warning("Etsy signature verification FAILED")
            raise HTTPException(status_code=401, detail="Signature verification failed")
    else:
        logger.info("Etsy webhook received without signature header (pre-approval test mode)")

    # 2. Parse payload
    payload: Dict[str, Any] = __import__("json").loads(raw_body)

    try:
        order_record = build_etsy_order_record(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    external_order_id = order_record.external_order_id

    # 3. Write to Neon
    try:
        order_id = _write_order_to_neon(order_record)
    except Exception as e:
        error_msg = str(e)
        if "unique" in error_msg.lower() or "duplicate" in error_msg.lower():
            logger.info(f"Duplicate Etsy order {external_order_id} — already in Neon, skipping")
            return {"status": "duplicate", "message": "Order already recorded"}
        logger.error(f"Neon write failed for Etsy order {external_order_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Database write failed: {e}")

    # 4. Log initial inventory entry
    try:
        _write_inventory_log(
            order_id=order_id,
            product_id=None,
            printful_order_id=None,
            fulfillment_status="pending_fulfillment",
        )
    except Exception as e:
        logger.warning(f"inventory_logs write failed (non-fatal): {e}")

    logger.info(
        "Etsy order %s processed — Neon order_id=%s, %s %s locked to AUD %s",
        external_order_id,
        order_id,
        order_record.currency_code,
        order_record.gross_amount_foreign,
        order_record.gross_amount_aud_locked,
    )

    return {
        "status": "ok",
        "platform": "Etsy",
        "external_order_id": external_order_id,
        "neon_order_id": order_id,
        "currency_code": order_record.currency_code,
        "gross_amount_aud_locked": str(order_record.gross_amount_aud_locked),
        "gst_reserve_aud": str(order_record.gst_reserve_aud),
    }


# ── Internal Agent Write Endpoints ──────────────────────────────────────────

def _get_pipeline_internal_key() -> str:
    """Retrieve the internal pipeline authentication key from Secret Manager."""
    from credentials import _get_secret
    return _get_secret("PIPELINE_INTERNAL_KEY")


def _verify_internal_key(authorization: str | None) -> bool:
    """Verify the Bearer token matches PIPELINE_INTERNAL_KEY."""
    if not authorization:
        return False
    try:
        scheme, token = authorization.split(" ", 1)
        if scheme.lower() != "bearer":
            return False
        expected = _get_pipeline_internal_key()
        return hmac.compare_digest(token.strip(), expected.strip())
    except Exception as e:
        logger.error(f"Internal key verification error: {e}")
        return False


@app.post("/internal/hook_library")
async def write_hook_library(
    request: Request,
    authorization: str = Header(None),
):
    """
    Internal endpoint: Agent 2 (Growth Hacker) writes a trend row to agent2_trend_research.
    Requires Bearer token matching PIPELINE_INTERNAL_KEY.

    Expected JSON body:
    {
        "keyword": str,
        "urgency_flag": str,           # "HOT", "MONITOR", or "WATCH"
        "trend_score": float,           # 0.0–100.0
        "source": str,                  # e.g. "pytrends", "google_cse"
        "region": str,                  # e.g. "AU", "US", "GB"
        "vibe_shift_brief": str,        # free text summary
        "cycle_date": str,              # ISO date string e.g. "2026-04-16"
        "raw_data": str                 # JSON string of raw source data
    }
    """
    if not _verify_internal_key(authorization):
        logger.warning("Unauthorised /internal/hook_library request")
        raise HTTPException(status_code=401, detail="Unauthorised")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    required_fields = ["keyword", "urgency_flag", "source", "region", "cycle_date"]
    for field in required_fields:
        if field not in body:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")

    engine = get_sqlalchemy_engine()
    try:
        with engine.begin() as conn:
            # Ensure agent2_trend_research table exists
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS agent2_trend_research (
                    id SERIAL PRIMARY KEY,
                    keyword TEXT NOT NULL,
                    urgency_flag TEXT NOT NULL CHECK (urgency_flag IN ('HOT', 'MONITOR', 'WATCH')),
                    trend_score FLOAT DEFAULT 0.0,
                    source TEXT NOT NULL,
                    region TEXT NOT NULL,
                    vibe_shift_brief TEXT,
                    cycle_date DATE NOT NULL,
                    raw_data TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            result = conn.execute(
                text("""
                    INSERT INTO agent2_trend_research
                    (keyword, urgency_flag, trend_score, source, region, vibe_shift_brief, cycle_date, raw_data)
                    VALUES (:keyword, :urgency_flag, :trend_score, :source, :region, :vibe_shift_brief, :cycle_date, :raw_data)
                    RETURNING id
                """),
                {
                    "keyword": body["keyword"],
                    "urgency_flag": body["urgency_flag"],
                    "trend_score": float(body.get("trend_score", 0.0)),
                    "source": body["source"],
                    "region": body["region"],
                    "vibe_shift_brief": body.get("vibe_shift_brief", ""),
                    "cycle_date": body["cycle_date"],
                    "raw_data": body.get("raw_data", ""),
                },
            )
            row = result.fetchone()
            new_id = row[0]
            logger.info(f"agent2_trend_research write OK — id={new_id}, keyword={body['keyword']}, flag={body['urgency_flag']}")
            return {"status": "ok", "id": new_id, "keyword": body["keyword"], "urgency_flag": body["urgency_flag"]}
    except Exception as e:
        logger.error(f"agent2_trend_research write failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database write failed: {e}")


@app.post("/internal/trend_shift")
async def write_trend_shift(
    request: Request,
    authorization: str = Header(None),
):
    """
    Internal endpoint: Agent 7 (Seasonal Pivot) writes a Vibe-Shift Brief to agent7_vibe_shift_briefs.
    Requires Bearer token matching PIPELINE_INTERNAL_KEY.

    Expected JSON body:
    {
        "brief_id": str,                # e.g. "VBS-005"
        "title": str,                   # e.g. "Quiet Mineral Biophilia"
        "summary": str,                 # full brief text
        "macro_signals": str,           # JSON string of macro signals list
        "activation_date": str,         # ISO date string e.g. "2026-04-16"
        "status": str,                  # "PENDING_OWNER_APPROVAL", "APPROVED", "REJECTED"
        "source_agent": str             # e.g. "Agent 7"
    }
    """
    if not _verify_internal_key(authorization):
        logger.warning("Unauthorised /internal/trend_shift request")
        raise HTTPException(status_code=401, detail="Unauthorised")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    required_fields = ["brief_id", "title", "summary", "activation_date", "status"]
    for field in required_fields:
        if field not in body:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")

    engine = get_sqlalchemy_engine()
    try:
        with engine.begin() as conn:
            # Ensure agent7_vibe_shift_briefs table exists
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS agent7_vibe_shift_briefs (
                    id SERIAL PRIMARY KEY,
                    brief_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    summary TEXT,
                    macro_signals TEXT,
                    activation_date DATE NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING_OWNER_APPROVAL',
                    source_agent TEXT DEFAULT 'Agent 7',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            result = conn.execute(
                text("""
                    INSERT INTO agent7_vibe_shift_briefs
                    (brief_id, title, summary, macro_signals, activation_date, status, source_agent)
                    VALUES (:brief_id, :title, :summary, :macro_signals, :activation_date, :status, :source_agent)
                    ON CONFLICT (brief_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        summary = EXCLUDED.summary,
                        macro_signals = EXCLUDED.macro_signals,
                        status = EXCLUDED.status,
                        created_at = NOW()
                    RETURNING id
                """),
                {
                    "brief_id": body["brief_id"],
                    "title": body["title"],
                    "summary": body["summary"],
                    "macro_signals": body.get("macro_signals", ""),
                    "activation_date": body["activation_date"],
                    "status": body["status"],
                    "source_agent": body.get("source_agent", "Agent 7"),
                },
            )
            row = result.fetchone()
            new_id = row[0]
            logger.info(f"agent7_vibe_shift_briefs write OK — id={new_id}, brief_id={body['brief_id']}, status={body['status']}")
            return {"status": "ok", "id": new_id, "brief_id": body["brief_id"], "title": body["title"]}
    except Exception as e:
        logger.error(f"agent7_vibe_shift_briefs write failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database write failed: {e}")


# ── Neon → Google Sheets Sync ────────────────────────────────────────────────

@app.post("/internal/sync_sheets")
async def sync_neon_to_sheets(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """
    Sync latest Neon data to Google Sheets Live-Pulse Engine.
    Reads agent2_trend_research and agent7_vibe_shift_briefs tables,
    then appends any rows not yet in Sheets to the appropriate tabs.
    Auth: Bearer {PIPELINE_INTERNAL_KEY}
    """
    if not _verify_internal_key(authorization):
        logger.warning("Unauthorised /internal/sync_sheets request")
        raise HTTPException(status_code=401, detail="Unauthorised")

    SPREADSHEET_ID = "1kCkgJv9tX6mdY2Q6ES6aWjMVhk_o4xbHtfHpFTSYLiU"
    HOOK_LIBRARY_TAB = "\U0001f3a3 Active Hook Library"
    TREND_SHIFT_TAB = "\U0001f4c5 Weekly Trend Shift Log"

    try:
        import json as _json
        import urllib.request as _urllib
        import urllib.parse as _urllib_parse
        import urllib.error as _urllib_error

        # Build Google Sheets API credentials from service account JSON stored in env
        sa_json_str = os.environ.get("GOOGLE_SHEETS_SERVICE_ACCT", "")
        if not sa_json_str:
            raise HTTPException(status_code=500, detail="GOOGLE_SHEETS_SERVICE_ACCT env var not set")

        sa_info = _json.loads(sa_json_str)

        # Get OAuth2 token using service account JWT
        import time as _time
        import jwt as _jwt  # PyJWT

        now = int(_time.time())
        payload = {
            "iss": sa_info["client_email"],
            "scope": "https://www.googleapis.com/auth/spreadsheets",
            "aud": "https://oauth2.googleapis.com/token",
            "iat": now,
            "exp": now + 3600,
        }
        private_key = sa_info["private_key"]
        signed_jwt = _jwt.encode(payload, private_key, algorithm="RS256")

        token_data = f"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion={signed_jwt}"
        token_req = _urllib.Request(
            "https://oauth2.googleapis.com/token",
            data=token_data.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with _urllib.urlopen(token_req) as resp:
            token_resp = _json.loads(resp.read())
        access_token = token_resp["access_token"]
        sheets_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        engine = get_sqlalchemy_engine()
        synced_hook = 0
        synced_brief = 0

        # ── Sync agent2_trend_research → Active Hook Library ──────────────────
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT keyword, urgency_flag, trend_score, source, region, vibe_shift_brief, cycle_date, created_at "
                "FROM agent2_trend_research ORDER BY created_at ASC"
            )).fetchall()

        if rows:
            append_values = []
            for row in rows:
                append_values.append([
                    str(row[7])[:10] if row[7] else "",  # Date Added
                    str(row[3]) if row[3] else "",        # Platform/Source
                    str(row[1]) if row[1] else "",        # Urgency Flag
                    str(row[0]) if row[0] else "",        # Keyword
                    str(row[5]) if row[5] else "",        # Vibe Shift Brief
                    str(row[2]) if row[2] else "",        # Trend Score
                    str(row[4]) if row[4] else "",        # Region
                    str(row[6])[:10] if row[6] else "",  # Cycle Date
                ])
            append_body = _json.dumps({"values": append_values, "majorDimension": "ROWS"}).encode()
            append_url = (
                f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}"
                f"/values/{_urllib_parse.quote(HOOK_LIBRARY_TAB + '!A:H')}:append"
                f"?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
            )
            append_req = _urllib.Request(append_url, data=append_body, headers=sheets_headers, method="POST")
            with _urllib.urlopen(append_req) as resp:
                _json.loads(resp.read())
            synced_hook = len(append_values)

        # ── Sync agent7_vibe_shift_briefs → Weekly Trend Shift Log ────────────
        with engine.connect() as conn:
            briefs = conn.execute(text(
                "SELECT brief_id, title, summary, macro_signals, activation_date, status, source_agent, created_at "
                "FROM agent7_vibe_shift_briefs ORDER BY created_at ASC"
            )).fetchall()

        if briefs:
            brief_values = []
            for b in briefs:
                brief_values.append([
                    str(b[7])[:10] if b[7] else "",  # Date Added
                    str(b[0]) if b[0] else "",        # Brief ID
                    str(b[1]) if b[1] else "",        # Title
                    str(b[2]) if b[2] else "",        # Summary
                    str(b[3]) if b[3] else "",        # Macro Signals
                    str(b[4])[:10] if b[4] else "",  # Activation Date
                    str(b[5]) if b[5] else "",        # Status
                    str(b[6]) if b[6] else "",        # Source Agent
                ])
            brief_body = _json.dumps({"values": brief_values, "majorDimension": "ROWS"}).encode()
            brief_url = (
                f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}"
                f"/values/{_urllib_parse.quote(TREND_SHIFT_TAB + '!A:H')}:append"
                f"?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
            )
            brief_req = _urllib.Request(brief_url, data=brief_body, headers=sheets_headers, method="POST")
            with _urllib.urlopen(brief_req) as resp:
                _json.loads(resp.read())
            synced_brief = len(brief_values)

        logger.info(f"sync_sheets OK — {synced_hook} trend rows, {synced_brief} brief rows synced")
        return {
            "status": "ok",
            "synced_trend_rows": synced_hook,
            "synced_brief_rows": synced_brief,
            "spreadsheet_id": SPREADSHEET_ID,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"sync_sheets failed: {e}")
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    logger.info("FFL Pipeline 1 starting up...")
    try:
        engine = get_sqlalchemy_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Neon database connection verified on startup")
    except Exception as e:
        logger.error(f"STARTUP WARNING: Could not connect to Neon on startup: {e}")
        logger.error("Pipeline will still start — connection will be retried per request")
