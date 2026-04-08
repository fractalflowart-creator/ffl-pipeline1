# FFL Pipeline 1 — Shopify/Etsy → Neon PostgreSQL

Webhook receiver for Fractal Flow Lab. Accepts order webhooks from Shopify and Etsy and writes order records directly to the Neon PostgreSQL database.

## Endpoints

- `GET /health` — Database connectivity check
- `POST /webhooks/shopify/orders/create` — Shopify order webhook (HMAC verified)
- `POST /webhooks/etsy/orders/create` — Etsy order webhook

## Environment Variables

Set these in Railway (never commit to the repo):

| Variable | Description |
|---|---|
| `NEON_DB_URL` | Neon PostgreSQL connection string |
| `SHOPIFY_WEBHOOK_SECRET` | Shopify webhook signing secret |

## Deployment

Deployed via Railway. Pushes to `main` branch trigger automatic redeployment.
