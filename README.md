# FFL Pipeline 1 — Shopify/Etsy → Neon PostgreSQL

Webhook receiver for Fractal Flow Lab. It accepts Shopify and Etsy order webhooks, writes order records directly to the Neon PostgreSQL database, and now includes the local Task E runtime-control layer for finance locking, approval-queue requests, and exception routing.

## Public Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Database connectivity check |
| `POST /webhooks/shopify/orders/create` | Shopify order webhook with HMAC verification and finance-lock write |
| `POST /webhooks/etsy/orders/create` | Etsy order webhook with signature verification or approved pre-live test mode |

## Authenticated Internal Endpoints

All internal endpoints require `Authorization: Bearer {PIPELINE_INTERNAL_KEY}`.

| Endpoint | Purpose |
|---|---|
| `POST /internal/hook_library` | Agent 2 trend-research write |
| `POST /internal/trend_shift` | Agent 7 vibe-shift brief write |
| `POST /internal/sync_sheets` | Mirror approved data from Neon into Google Sheets |
| `POST /internal/approval_queue/request` | Open or deduplicate a governed approval item |
| `POST /internal/approval_queue/check` | Check whether a governed approval item exists and is approved |
| `POST /internal/order_exceptions/open` | Open or deduplicate a fulfillment or control exception |
| `POST /internal/order_exceptions/customer_notification` | Advance the customer-notification state for an open exception |
| `POST /internal/orders/finance_enrichment` | Update post-ingest fee, cost, profit, and fulfillment-state fields |

## Environment Variables

Set these in the deployment environment and never commit them to the repo.

| Variable | Required | Description |
|---|---|---|
| `FFL_SECRET_SOURCE` | Yes | Explicit secret-loading mode: `gcp_secret_manager` or `env` |
| `NEON_DB_URL` | Yes | Neon PostgreSQL connection string |
| `SHOPIFY_WEBHOOK_SECRET` | Yes for Shopify | Shopify webhook signing secret |
| `ETSY_SHARED_SECRET` | Yes for live Etsy verification | Etsy webhook signing secret |
| `PIPELINE_INTERNAL_KEY` | Yes for internal routes | Bearer token used by authenticated internal control routes |
| `FFL_DB_SSL_MODE` | Optional | `require` for Neon or `disable` for local PostgreSQL staging validation |
| `FFL_GST_MODE` | Optional | `none` or `inclusive`; controls order-level GST reserve logic |
| `FFL_FX_RATE_OVERRIDES` | Optional | JSON object mapping foreign currency codes to locked AUD rates for staging or controlled fallback, for example `{"USD": 1.52}` |
| `GOOGLE_SHEETS_SERVICE_ACCT` | Required for `/internal/sync_sheets` | Service-account JSON used for Sheets sync |

## Runtime Notes

The local Task E refactor is deliberately non-destructive. It assumes the Task D schema migration has been applied in a safe staging or live environment before the new order-finance columns and control tables are used. It also keeps `total_amount` populated as the backward-compatible AUD mirror of `gross_amount_aud_locked`.

The approval-request and exception-open paths are now available inside this service, but owner-authenticated approval resolution is intentionally left outside the webhook runtime. That control surface should remain a separately governed step before production automation is widened.

## Deployment

The service is deployed via Railway. Pushes to `main` trigger automatic redeployment. Because the live Neon migration is still a separate explicit approval step, local code changes should be validated in a safe staging path before any production deployment or database execution.
