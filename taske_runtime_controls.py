from __future__ import annotations

import hmac
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import text

from credentials import _get_secret, get_sqlalchemy_engine

logger = logging.getLogger("ffl.pipeline1.taske")
router = APIRouter()

MONEY_QUANTUM = Decimal("0.01")
RATE_QUANTUM = Decimal("0.00000001")
APPROVAL_RISK_LEVELS = {"low", "medium", "high", "critical"}
APPROVAL_STATUSES = {"pending", "approved", "rejected", "revision_requested", "expired"}
EXCEPTION_SEVERITIES = {"low", "medium", "high", "critical"}
CUSTOMER_NOTIFICATION_STATUSES = {
    "not_needed",
    "draft_pending",
    "awaiting_owner_approval",
    "sent",
    "closed_no_send",
}
EXCEPTION_RESOLUTION_STATUSES = {"open", "owner_review", "customer_contacted", "resolved"}
ORDER_STATUS_VALUES = {
    "received",
    "fx_locked",
    "reserve_booked",
    "fulfillment_pending",
    "in_production",
    "shipped",
    "completed",
}


@dataclass(slots=True)
class OrderFinanceRecord:
    external_order_id: str
    platform: str
    customer_email: str
    ordered_at: datetime
    currency_code: str
    gross_amount_foreign: Decimal
    fx_rate_to_aud_locked: Decimal
    gross_amount_aud_locked: Decimal
    gst_reserve_aud: Decimal
    platform_fee_aud: Decimal | None = None
    payment_fee_aud: Decimal | None = None
    printful_cost_aud: Decimal | None = None
    net_profit_aud_estimate: Decimal | None = None
    status: str = "fulfillment_pending"


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _quantize_rate(value: Decimal) -> Decimal:
    return value.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)


def _decimal_or_none(value: Any, *, divisor: Any | None = None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        decimal_value = Decimal(str(value))
        if divisor not in (None, "", 0, "0"):
            decimal_value = decimal_value / Decimal(str(divisor))
        return decimal_value
    except (InvalidOperation, ValueError, TypeError):
        return None


def _decimal_required(value: Any, *, divisor: Any | None = None, default: str = "0") -> Decimal:
    resolved = _decimal_or_none(value, divisor=divisor)
    if resolved is None:
        return Decimal(default)
    return resolved


def _normalize_currency_code(value: Any) -> str:
    if not value:
        return "AUD"
    text_value = str(value).strip().upper()
    if len(text_value) != 3:
        return "AUD"
    return text_value


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    if value:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _get_fx_rate_overrides() -> Dict[str, Decimal]:
    raw = os.environ.get("FFL_FX_RATE_OVERRIDES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FFL_FX_RATE_OVERRIDES is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("FFL_FX_RATE_OVERRIDES must be a JSON object mapping currency codes to AUD rates")

    result: Dict[str, Decimal] = {}
    for key, value in parsed.items():
        rate_value = _decimal_or_none(value)
        if rate_value is None or rate_value <= 0:
            raise RuntimeError(f"Invalid FX override for currency '{key}'")
        result[_normalize_currency_code(key)] = _quantize_rate(rate_value)
    return result


def _resolve_locked_fx_rate(
    currency_code: str,
    gross_amount_foreign: Decimal,
    *,
    aud_amount_hint: Decimal | None = None,
) -> Decimal:
    if currency_code == "AUD":
        return Decimal("1.0")

    if aud_amount_hint is not None and gross_amount_foreign > 0 and aud_amount_hint > 0:
        return _quantize_rate(aud_amount_hint / gross_amount_foreign)

    overrides = _get_fx_rate_overrides()
    if currency_code in overrides:
        return overrides[currency_code]

    raise ValueError(
        f"Unable to resolve locked FX rate for currency '{currency_code}'. "
        "Provide an AUD amount in the payload path or configure FFL_FX_RATE_OVERRIDES for staging."
    )


def _get_gst_mode() -> str:
    mode = os.environ.get("FFL_GST_MODE", "none").strip().lower()
    if mode not in {"none", "inclusive"}:
        raise RuntimeError("FFL_GST_MODE must be either 'none' or 'inclusive'")
    return mode


def _calculate_gst_reserve(gross_amount_aud_locked: Decimal) -> Decimal:
    if _get_gst_mode() != "inclusive":
        return Decimal("0.00")
    return _quantize_money(gross_amount_aud_locked / Decimal("11"))


def _calculate_net_profit(
    gross_amount_aud_locked: Decimal,
    gst_reserve_aud: Decimal,
    platform_fee_aud: Decimal | None,
    payment_fee_aud: Decimal | None,
    printful_cost_aud: Decimal | None,
) -> Decimal | None:
    if platform_fee_aud is None or payment_fee_aud is None or printful_cost_aud is None:
        return None
    net = gross_amount_aud_locked - gst_reserve_aud - platform_fee_aud - payment_fee_aud - printful_cost_aud
    return _quantize_money(net)


def _extract_shopify_money_set(payload: Dict[str, Any]) -> tuple[Decimal | None, str | None, Decimal | None, str | None]:
    money_set = payload.get("current_total_price_set") or payload.get("total_price_set") or {}
    shop_money = money_set.get("shop_money") or {}
    presentment_money = money_set.get("presentment_money") or {}

    foreign_amount = _decimal_or_none(presentment_money.get("amount"))
    foreign_currency = _normalize_currency_code(
        presentment_money.get("currency_code") or payload.get("presentment_currency") or payload.get("currency")
    )

    aud_amount = _decimal_or_none(shop_money.get("amount"))
    aud_currency = _normalize_currency_code(shop_money.get("currency_code") or "AUD") if shop_money else None

    return foreign_amount, foreign_currency, aud_amount, aud_currency


def build_shopify_order_record(payload: Dict[str, Any]) -> OrderFinanceRecord:
    external_order_id = str(payload.get("id", "")).strip()
    if not external_order_id:
        raise ValueError("Missing Shopify order id in payload")

    customer = payload.get("customer") or {}
    customer_email = str(customer.get("email") or payload.get("email") or "").strip()
    ordered_at = _parse_datetime(payload.get("created_at"))

    foreign_amount_hint, foreign_currency_hint, aud_amount_hint, aud_currency_hint = _extract_shopify_money_set(payload)
    fallback_total = _decimal_required(payload.get("total_price"), default="0")
    currency_code = _normalize_currency_code(foreign_currency_hint or payload.get("currency") or payload.get("presentment_currency"))
    gross_amount_foreign = _quantize_money(foreign_amount_hint if foreign_amount_hint is not None else fallback_total)

    aud_hint = aud_amount_hint if aud_amount_hint is not None and aud_currency_hint == "AUD" else None
    fx_rate = _resolve_locked_fx_rate(currency_code, gross_amount_foreign, aud_amount_hint=aud_hint)
    gross_amount_aud_locked = _quantize_money(aud_hint if aud_hint is not None else gross_amount_foreign * fx_rate)
    gst_reserve_aud = _calculate_gst_reserve(gross_amount_aud_locked)

    return OrderFinanceRecord(
        external_order_id=external_order_id,
        platform="Shopify",
        customer_email=customer_email,
        ordered_at=ordered_at,
        currency_code=currency_code,
        gross_amount_foreign=gross_amount_foreign,
        fx_rate_to_aud_locked=fx_rate,
        gross_amount_aud_locked=gross_amount_aud_locked,
        gst_reserve_aud=gst_reserve_aud,
        platform_fee_aud=None,
        payment_fee_aud=None,
        printful_cost_aud=None,
        net_profit_aud_estimate=None,
        status="fulfillment_pending",
    )


def build_etsy_order_record(payload: Dict[str, Any]) -> OrderFinanceRecord:
    receipt = payload.get("receipt", payload)
    external_order_id = str(receipt.get("receipt_id", receipt.get("id", ""))).strip()
    if not external_order_id:
        raise ValueError("Missing Etsy receipt_id in payload")

    customer_email = str(receipt.get("buyer_email") or receipt.get("email") or "").strip()
    ordered_at = _parse_datetime(receipt.get("create_timestamp") or receipt.get("created_timestamp"))

    grandtotal = receipt.get("grandtotal") or {}
    divisor = grandtotal.get("divisor", 100)
    gross_amount_foreign = _decimal_or_none(grandtotal.get("amount"), divisor=divisor)
    if gross_amount_foreign is None or gross_amount_foreign == 0:
        gross_amount_foreign = _decimal_required(receipt.get("total_price"), default="0")
    gross_amount_foreign = _quantize_money(gross_amount_foreign)

    currency_code = _normalize_currency_code(
        receipt.get("currency_code") or grandtotal.get("currency_code") or receipt.get("currency")
    )

    aud_amount_hint = _decimal_or_none(receipt.get("gross_amount_aud_locked") or payload.get("gross_amount_aud_locked"))
    fx_rate = _resolve_locked_fx_rate(currency_code, gross_amount_foreign, aud_amount_hint=aud_amount_hint)
    gross_amount_aud_locked = _quantize_money(aud_amount_hint if aud_amount_hint is not None else gross_amount_foreign * fx_rate)
    gst_reserve_aud = _calculate_gst_reserve(gross_amount_aud_locked)

    return OrderFinanceRecord(
        external_order_id=external_order_id,
        platform="Etsy",
        customer_email=customer_email,
        ordered_at=ordered_at,
        currency_code=currency_code,
        gross_amount_foreign=gross_amount_foreign,
        fx_rate_to_aud_locked=fx_rate,
        gross_amount_aud_locked=gross_amount_aud_locked,
        gst_reserve_aud=gst_reserve_aud,
        platform_fee_aud=None,
        payment_fee_aud=None,
        printful_cost_aud=None,
        net_profit_aud_estimate=None,
        status="fulfillment_pending",
    )


def create_finance_locked_order(order_record: OrderFinanceRecord) -> int:
    engine = get_sqlalchemy_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                INSERT INTO orders (
                    external_order_id,
                    platform,
                    customer_email,
                    total_amount,
                    status,
                    ordered_at,
                    currency_code,
                    gross_amount_foreign,
                    fx_rate_to_aud_locked,
                    gross_amount_aud_locked,
                    gst_reserve_aud,
                    platform_fee_aud,
                    payment_fee_aud,
                    printful_cost_aud,
                    net_profit_aud_estimate
                ) VALUES (
                    :external_order_id,
                    :platform,
                    :customer_email,
                    :total_amount,
                    :status,
                    :ordered_at,
                    :currency_code,
                    :gross_amount_foreign,
                    :fx_rate_to_aud_locked,
                    :gross_amount_aud_locked,
                    :gst_reserve_aud,
                    :platform_fee_aud,
                    :payment_fee_aud,
                    :printful_cost_aud,
                    :net_profit_aud_estimate
                )
                RETURNING order_id
                """
            ),
            {
                "external_order_id": order_record.external_order_id,
                "platform": order_record.platform,
                "customer_email": order_record.customer_email,
                "total_amount": order_record.gross_amount_aud_locked,
                "status": order_record.status,
                "ordered_at": order_record.ordered_at,
                "currency_code": order_record.currency_code,
                "gross_amount_foreign": order_record.gross_amount_foreign,
                "fx_rate_to_aud_locked": order_record.fx_rate_to_aud_locked,
                "gross_amount_aud_locked": order_record.gross_amount_aud_locked,
                "gst_reserve_aud": order_record.gst_reserve_aud,
                "platform_fee_aud": order_record.platform_fee_aud,
                "payment_fee_aud": order_record.payment_fee_aud,
                "printful_cost_aud": order_record.printful_cost_aud,
                "net_profit_aud_estimate": order_record.net_profit_aud_estimate,
            },
        )
        row = result.fetchone()
        order_id = row[0]
        logger.info(
            "Finance-locked order write OK — orders.order_id=%s (%s #%s, %s %s -> AUD %s)",
            order_id,
            order_record.platform,
            order_record.external_order_id,
            order_record.currency_code,
            order_record.gross_amount_foreign,
            order_record.gross_amount_aud_locked,
        )
        return order_id


def write_inventory_log_record(
    order_id: int,
    product_id: int | None,
    printful_order_id: str | None,
    fulfillment_status: str,
) -> None:
    engine = get_sqlalchemy_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO inventory_logs (
                    order_id,
                    product_id,
                    printful_order_id,
                    fulfillment_status
                ) VALUES (
                    :order_id,
                    :product_id,
                    :printful_order_id,
                    :fulfillment_status
                )
                """
            ),
            {
                "order_id": order_id,
                "product_id": product_id,
                "printful_order_id": printful_order_id,
                "fulfillment_status": fulfillment_status,
            },
        )
        logger.info("Inventory log write OK — order_id=%s, fulfillment_status=%s", order_id, fulfillment_status)


def _get_pipeline_internal_key() -> str:
    return _get_secret("PIPELINE_INTERNAL_KEY")


def verify_internal_key(authorization: str | None) -> bool:
    if not authorization:
        return False
    try:
        scheme, token = authorization.split(" ", 1)
        if scheme.lower() != "bearer":
            return False
        expected = _get_pipeline_internal_key()
        return hmac.compare_digest(token.strip(), expected.strip())
    except Exception as exc:
        logger.error("Internal key verification error: %s", exc)
        return False


def _request_approval(
    *,
    entity_type: str,
    entity_id: str,
    approval_type: str,
    reason: str,
    risk_level: str,
    requested_by_agent: str,
) -> Dict[str, Any]:
    normalized_risk = str(risk_level).strip().lower()
    if normalized_risk not in APPROVAL_RISK_LEVELS:
        raise ValueError("risk_level must be one of low, medium, high, or critical")

    engine = get_sqlalchemy_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            text(
                """
                SELECT approval_id, status, requested_at
                FROM approval_queue
                WHERE entity_type = :entity_type
                  AND entity_id = :entity_id
                  AND approval_type = :approval_type
                  AND status IN ('pending', 'revision_requested')
                ORDER BY requested_at DESC
                LIMIT 1
                """
            ),
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "approval_type": approval_type,
            },
        ).mappings().first()
        if existing:
            return {
                "approval_id": existing["approval_id"],
                "status": existing["status"],
                "deduplicated": True,
            }

        inserted = conn.execute(
            text(
                """
                INSERT INTO approval_queue (
                    entity_type,
                    entity_id,
                    approval_type,
                    reason,
                    risk_level,
                    requested_by_agent,
                    status
                ) VALUES (
                    :entity_type,
                    :entity_id,
                    :approval_type,
                    :reason,
                    :risk_level,
                    :requested_by_agent,
                    'pending'
                )
                RETURNING approval_id, status
                """
            ),
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "approval_type": approval_type,
                "reason": reason,
                "risk_level": normalized_risk,
                "requested_by_agent": requested_by_agent,
            },
        ).mappings().first()

        logger.info(
            "Approval requested — approval_id=%s, entity=%s:%s, type=%s",
            inserted["approval_id"],
            entity_type,
            entity_id,
            approval_type,
        )
        return {
            "approval_id": inserted["approval_id"],
            "status": inserted["status"],
            "deduplicated": False,
        }


def _check_approval(entity_type: str, entity_id: str, approval_type: str) -> Dict[str, Any]:
    engine = get_sqlalchemy_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT approval_id, status, requested_at, resolved_at, owner_note
                FROM approval_queue
                WHERE entity_type = :entity_type
                  AND entity_id = :entity_id
                  AND approval_type = :approval_type
                ORDER BY requested_at DESC
                LIMIT 1
                """
            ),
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "approval_type": approval_type,
            },
        ).mappings().first()

    if not row:
        return {
            "exists": False,
            "approved": False,
            "status": None,
            "approval_id": None,
        }

    return {
        "exists": True,
        "approved": row["status"] == "approved",
        "status": row["status"],
        "approval_id": row["approval_id"],
        "requested_at": row["requested_at"].isoformat() if row["requested_at"] else None,
        "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] else None,
        "owner_note": row["owner_note"],
    }


def _open_order_exception(
    *,
    order_id: int,
    platform: str,
    exception_type: str,
    severity: str,
    customer_notification_status: str = "draft_pending",
    resolution_note: str | None = None,
) -> Dict[str, Any]:
    normalized_severity = str(severity).strip().lower()
    if normalized_severity not in EXCEPTION_SEVERITIES:
        raise ValueError("severity must be one of low, medium, high, or critical")

    normalized_notification_status = str(customer_notification_status).strip().lower()
    if normalized_notification_status not in CUSTOMER_NOTIFICATION_STATUSES:
        raise ValueError(
            "customer_notification_status must be one of not_needed, draft_pending, awaiting_owner_approval, sent, or closed_no_send"
        )

    resolution_status = "owner_review" if normalized_severity in {"high", "critical"} else "open"
    engine = get_sqlalchemy_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            text(
                """
                SELECT exception_id, resolution_status, customer_notification_status
                FROM order_exceptions
                WHERE order_id = :order_id
                  AND exception_type = :exception_type
                  AND resolution_status <> 'resolved'
                ORDER BY exception_id DESC
                LIMIT 1
                """
            ),
            {
                "order_id": order_id,
                "exception_type": exception_type,
            },
        ).mappings().first()
        if existing:
            return {
                "exception_id": existing["exception_id"],
                "resolution_status": existing["resolution_status"],
                "customer_notification_status": existing["customer_notification_status"],
                "deduplicated": True,
            }

        inserted = conn.execute(
            text(
                """
                INSERT INTO order_exceptions (
                    order_id,
                    platform,
                    exception_type,
                    severity,
                    customer_notification_status,
                    owner_notified_at,
                    resolution_status,
                    resolution_note
                ) VALUES (
                    :order_id,
                    :platform,
                    :exception_type,
                    :severity,
                    :customer_notification_status,
                    CASE WHEN :resolution_status = 'owner_review' THEN NOW() ELSE NULL END,
                    :resolution_status,
                    :resolution_note
                )
                RETURNING exception_id, resolution_status, customer_notification_status
                """
            ),
            {
                "order_id": order_id,
                "platform": platform,
                "exception_type": exception_type,
                "severity": normalized_severity,
                "customer_notification_status": normalized_notification_status,
                "resolution_status": resolution_status,
                "resolution_note": resolution_note,
            },
        ).mappings().first()

    if normalized_severity in {"high", "critical"}:
        _request_approval(
            entity_type="exception",
            entity_id=str(inserted["exception_id"]),
            approval_type="exception_resolution",
            reason=f"High-severity order exception '{exception_type}' requires governed resolution.",
            risk_level=normalized_severity,
            requested_by_agent="Pipeline 1",
        )

    logger.info(
        "Order exception opened — exception_id=%s, order_id=%s, type=%s, severity=%s",
        inserted["exception_id"],
        order_id,
        exception_type,
        normalized_severity,
    )
    return {
        "exception_id": inserted["exception_id"],
        "resolution_status": inserted["resolution_status"],
        "customer_notification_status": inserted["customer_notification_status"],
        "deduplicated": False,
    }


def _update_order_exception_notification(
    *,
    exception_id: int,
    customer_notification_status: str,
    resolution_status: str | None = None,
    resolution_note: str | None = None,
) -> Dict[str, Any]:
    normalized_notification_status = str(customer_notification_status).strip().lower()
    if normalized_notification_status not in CUSTOMER_NOTIFICATION_STATUSES:
        raise ValueError(
            "customer_notification_status must be one of not_needed, draft_pending, awaiting_owner_approval, sent, or closed_no_send"
        )

    normalized_resolution_status = None
    if resolution_status is not None:
        normalized_resolution_status = str(resolution_status).strip().lower()
        if normalized_resolution_status not in EXCEPTION_RESOLUTION_STATUSES:
            raise ValueError("resolution_status must be one of open, owner_review, customer_contacted, or resolved")

    engine = get_sqlalchemy_engine()
    with engine.begin() as conn:
        updated = conn.execute(
            text(
                """
                UPDATE order_exceptions
                SET customer_notification_status = :customer_notification_status,
                    resolution_status = COALESCE(:resolution_status, resolution_status),
                    resolution_note = COALESCE(:resolution_note, resolution_note)
                WHERE exception_id = :exception_id
                RETURNING exception_id, customer_notification_status, resolution_status, resolution_note
                """
            ),
            {
                "exception_id": exception_id,
                "customer_notification_status": normalized_notification_status,
                "resolution_status": normalized_resolution_status,
                "resolution_note": resolution_note,
            },
        ).mappings().first()
        if not updated:
            raise ValueError(f"No order_exceptions row found for exception_id={exception_id}")

    logger.info(
        "Order exception notification updated — exception_id=%s, customer_notification_status=%s, resolution_status=%s",
        updated["exception_id"],
        updated["customer_notification_status"],
        updated["resolution_status"],
    )
    return dict(updated)


def _update_order_finance_enrichment(
    *,
    order_id: int,
    platform_fee_aud: Any | None,
    payment_fee_aud: Any | None,
    printful_cost_aud: Any | None,
    status: str | None,
) -> Dict[str, Any]:
    platform_fee = _decimal_or_none(platform_fee_aud)
    payment_fee = _decimal_or_none(payment_fee_aud)
    printful_cost = _decimal_or_none(printful_cost_aud)
    if platform_fee is not None:
        platform_fee = _quantize_money(platform_fee)
    if payment_fee is not None:
        payment_fee = _quantize_money(payment_fee)
    if printful_cost is not None:
        printful_cost = _quantize_money(printful_cost)

    normalized_status = None
    if status is not None:
        normalized_status = str(status).strip().lower()
        if normalized_status not in ORDER_STATUS_VALUES:
            raise ValueError("status must be one of received, fx_locked, reserve_booked, fulfillment_pending, in_production, shipped, or completed")

    engine = get_sqlalchemy_engine()
    with engine.begin() as conn:
        current = conn.execute(
            text(
                """
                SELECT gross_amount_aud_locked, gst_reserve_aud, platform_fee_aud, payment_fee_aud, printful_cost_aud
                FROM orders
                WHERE order_id = :order_id
                """
            ),
            {"order_id": order_id},
        ).mappings().first()
        if not current:
            raise ValueError(f"No orders row found for order_id={order_id}")

        effective_platform_fee = platform_fee if platform_fee is not None else current["platform_fee_aud"]
        effective_payment_fee = payment_fee if payment_fee is not None else current["payment_fee_aud"]
        effective_printful_cost = printful_cost if printful_cost is not None else current["printful_cost_aud"]
        net_profit = _calculate_net_profit(
            gross_amount_aud_locked=_decimal_required(current["gross_amount_aud_locked"]),
            gst_reserve_aud=_decimal_required(current["gst_reserve_aud"]),
            platform_fee_aud=_decimal_or_none(effective_platform_fee),
            payment_fee_aud=_decimal_or_none(effective_payment_fee),
            printful_cost_aud=_decimal_or_none(effective_printful_cost),
        )

        updated = conn.execute(
            text(
                """
                UPDATE orders
                SET platform_fee_aud = COALESCE(:platform_fee_aud, platform_fee_aud),
                    payment_fee_aud = COALESCE(:payment_fee_aud, payment_fee_aud),
                    printful_cost_aud = COALESCE(:printful_cost_aud, printful_cost_aud),
                    net_profit_aud_estimate = :net_profit_aud_estimate,
                    status = COALESCE(:status, status)
                WHERE order_id = :order_id
                RETURNING order_id, status, platform_fee_aud, payment_fee_aud, printful_cost_aud, net_profit_aud_estimate
                """
            ),
            {
                "order_id": order_id,
                "platform_fee_aud": platform_fee,
                "payment_fee_aud": payment_fee,
                "printful_cost_aud": printful_cost,
                "net_profit_aud_estimate": net_profit,
                "status": normalized_status,
            },
        ).mappings().first()

    logger.info("Order finance enrichment updated — order_id=%s, status=%s", updated["order_id"], updated["status"])
    return {
        "order_id": updated["order_id"],
        "status": updated["status"],
        "platform_fee_aud": str(updated["platform_fee_aud"]) if updated["platform_fee_aud"] is not None else None,
        "payment_fee_aud": str(updated["payment_fee_aud"]) if updated["payment_fee_aud"] is not None else None,
        "printful_cost_aud": str(updated["printful_cost_aud"]) if updated["printful_cost_aud"] is not None else None,
        "net_profit_aud_estimate": str(updated["net_profit_aud_estimate"]) if updated["net_profit_aud_estimate"] is not None else None,
    }


@router.post("/internal/approval_queue/request")
async def request_approval_item(
    request: Request,
    authorization: str = Header(None),
):
    if not verify_internal_key(authorization):
        logger.warning("Unauthorised /internal/approval_queue/request")
        raise HTTPException(status_code=401, detail="Unauthorised")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    required_fields = ["entity_type", "entity_id", "approval_type", "reason", "risk_level", "requested_by_agent"]
    for field in required_fields:
        if field not in body:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")

    try:
        result = _request_approval(
            entity_type=str(body["entity_type"]),
            entity_id=str(body["entity_id"]),
            approval_type=str(body["approval_type"]),
            reason=str(body["reason"]),
            risk_level=str(body["risk_level"]),
            requested_by_agent=str(body["requested_by_agent"]),
        )
        return {"status": "ok", **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Approval request failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Approval request failed: {exc}") from exc


@router.post("/internal/approval_queue/check")
async def check_approval_item(
    request: Request,
    authorization: str = Header(None),
):
    if not verify_internal_key(authorization):
        logger.warning("Unauthorised /internal/approval_queue/check")
        raise HTTPException(status_code=401, detail="Unauthorised")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    required_fields = ["entity_type", "entity_id", "approval_type"]
    for field in required_fields:
        if field not in body:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")

    try:
        result = _check_approval(
            entity_type=str(body["entity_type"]),
            entity_id=str(body["entity_id"]),
            approval_type=str(body["approval_type"]),
        )
        return {"status": "ok", **result}
    except Exception as exc:
        logger.error("Approval check failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Approval check failed: {exc}") from exc


@router.post("/internal/order_exceptions/open")
async def open_order_exception(
    request: Request,
    authorization: str = Header(None),
):
    if not verify_internal_key(authorization):
        logger.warning("Unauthorised /internal/order_exceptions/open")
        raise HTTPException(status_code=401, detail="Unauthorised")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    required_fields = ["order_id", "platform", "exception_type", "severity"]
    for field in required_fields:
        if field not in body:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")

    try:
        result = _open_order_exception(
            order_id=int(body["order_id"]),
            platform=str(body["platform"]),
            exception_type=str(body["exception_type"]),
            severity=str(body["severity"]),
            customer_notification_status=str(body.get("customer_notification_status", "draft_pending")),
            resolution_note=body.get("resolution_note"),
        )
        return {"status": "ok", **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Order exception open failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Order exception open failed: {exc}") from exc


@router.post("/internal/order_exceptions/customer_notification")
async def update_order_exception_customer_notification(
    request: Request,
    authorization: str = Header(None),
):
    if not verify_internal_key(authorization):
        logger.warning("Unauthorised /internal/order_exceptions/customer_notification")
        raise HTTPException(status_code=401, detail="Unauthorised")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    required_fields = ["exception_id", "customer_notification_status"]
    for field in required_fields:
        if field not in body:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")

    try:
        result = _update_order_exception_notification(
            exception_id=int(body["exception_id"]),
            customer_notification_status=str(body["customer_notification_status"]),
            resolution_status=body.get("resolution_status"),
            resolution_note=body.get("resolution_note"),
        )
        return {"status": "ok", **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Order exception notification update failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Order exception notification update failed: {exc}") from exc


@router.post("/internal/orders/finance_enrichment")
async def update_order_finance_enrichment(
    request: Request,
    authorization: str = Header(None),
):
    if not verify_internal_key(authorization):
        logger.warning("Unauthorised /internal/orders/finance_enrichment")
        raise HTTPException(status_code=401, detail="Unauthorised")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if "order_id" not in body:
        raise HTTPException(status_code=400, detail="Missing required field: order_id")

    try:
        result = _update_order_finance_enrichment(
            order_id=int(body["order_id"]),
            platform_fee_aud=body.get("platform_fee_aud"),
            payment_fee_aud=body.get("payment_fee_aud"),
            printful_cost_aud=body.get("printful_cost_aud"),
            status=body.get("status"),
        )
        return {"status": "ok", **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Order finance enrichment update failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Order finance enrichment update failed: {exc}") from exc
