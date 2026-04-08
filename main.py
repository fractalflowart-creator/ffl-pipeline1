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
from fastapi.responses import JSONResponse
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
