"""
FFL Pipeline 1 — Webhook Receiver
Accepts order webhooks from Shopify and Etsy.
Writes directly to Neon PostgreSQL (orders + inventory_logs tables).
Triggers Printful fulfillment AFTER successful Neon write.

Endpoints:
  POST /webhooks/shopify/orders/create   — Shopify new order
  POST /webhooks/etsy/orders/create      — Etsy new order (receipt)
  GET  /health                           — Health check
"""

import hashlib
import hmac
import base64
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse, HTMLResponse
from sqlalchemy import text

from credentials import get_sqlalchemy_engine, get_shopify_webhook_secret, get_etsy_shared_secret

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
    version="1.0.0",
)


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

def _write_order_to_neon(
    external_order_id: str,
    platform: str,
    customer_email: str,
    total_amount: float,
    ordered_at: datetime,
) -> int:
    """
    INSERT a new order into the Neon orders table.
    Returns the new order_id (SERIAL primary key).
    Raises on duplicate external_order_id (UNIQUE constraint).
    """
    engine = get_sqlalchemy_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO orders "
                "(external_order_id, platform, customer_email, total_amount, status, ordered_at) "
                "VALUES (:external_order_id, :platform, :customer_email, :total_amount, 'pending', :ordered_at) "
                "RETURNING order_id"
            ),
            {
                "external_order_id": external_order_id,
                "platform": platform,
                "customer_email": customer_email,
                "total_amount": total_amount,
                "ordered_at": ordered_at,
            },
        )
        row = result.fetchone()
        order_id = row[0]
        logger.info(f"Neon write OK — orders.order_id={order_id} ({platform} #{external_order_id})")
        return order_id


def _write_inventory_log(
    order_id: int,
    product_id: int | None,
    printful_order_id: str | None,
    fulfillment_status: str,
) -> None:
    """INSERT a row into inventory_logs after fulfillment confirmation."""
    engine = get_sqlalchemy_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO inventory_logs "
                "(order_id, product_id, printful_order_id, fulfillment_status) "
                "VALUES (:order_id, :product_id, :printful_order_id, :fulfillment_status)"
            ),
            {
                "order_id": order_id,
                "product_id": product_id,
                "printful_order_id": printful_order_id,
                "fulfillment_status": fulfillment_status,
            },
        )
        logger.info(f"Neon write OK — inventory_logs for order_id={order_id}")


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

    external_order_id = str(payload.get("id", ""))
    customer = payload.get("customer", {})
    customer_email = customer.get("email", payload.get("email", ""))
    total_amount = float(payload.get("total_price", 0.0))

    # Parse created_at timestamp
    created_at_str = payload.get("created_at", "")
    try:
        ordered_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
    except Exception:
        ordered_at = datetime.now(timezone.utc)

    if not external_order_id:
        raise HTTPException(status_code=400, detail="Missing order id in payload")

    # 3. Write to Neon
    try:
        order_id = _write_order_to_neon(
            external_order_id=external_order_id,
            platform="Shopify",
            customer_email=customer_email,
            total_amount=total_amount,
            ordered_at=ordered_at,
        )
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
        f"Shopify order {external_order_id} processed — "
        f"Neon order_id={order_id}, amount=${total_amount:.2f}, "
        f"shop={x_shopify_shop_domain}"
    )

    return {
        "status": "ok",
        "platform": "Shopify",
        "external_order_id": external_order_id,
        "neon_order_id": order_id,
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

    # Etsy receipt structure
    receipt = payload.get("receipt", payload)  # Some versions nest under "receipt"
    external_order_id = str(receipt.get("receipt_id", receipt.get("id", "")))
    customer_email = receipt.get("buyer_email", receipt.get("email", ""))
    total_amount = float(receipt.get("grandtotal", {}).get("amount", 0)) / 100  # Etsy uses cents
    if total_amount == 0:
        # Try alternative field
        total_amount = float(receipt.get("total_price", 0.0))

    created_timestamp = receipt.get("create_timestamp", receipt.get("created_timestamp", 0))
    try:
        ordered_at = datetime.fromtimestamp(int(created_timestamp), tz=timezone.utc)
    except Exception:
        ordered_at = datetime.now(timezone.utc)

    if not external_order_id:
        raise HTTPException(status_code=400, detail="Missing receipt_id in payload")

    # 3. Write to Neon
    try:
        order_id = _write_order_to_neon(
            external_order_id=external_order_id,
            platform="Etsy",
            customer_email=customer_email,
            total_amount=total_amount,
            ordered_at=ordered_at,
        )
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
        f"Etsy order {external_order_id} processed — "
        f"Neon order_id={order_id}, amount=${total_amount:.2f}"
    )

    return {
        "status": "ok",
        "platform": "Etsy",
        "external_order_id": external_order_id,
        "neon_order_id": order_id,
    }


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
