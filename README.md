# Amazon Operations Suite

A three-section Flask app for Amazon sellers. A landing page (`/`) lets you pick a tool:

1. **N-Gram Analyzer** (`/ngram`) — upload a PPC search-term report and surface
   negative-keyword candidates from mono/bi/tri-grams. (Original tool, unchanged.)
2. **Inventory Transfer** (`/inventory`) — connect Seller Central via Amazon OAuth,
   auto-pull three SP-API reports, and build the formula-driven 4-sheet FBA Inventory
   Transfer workbook (per `amazon_fba_inventory_transfer_skill.md`). Bands are
   auto-assigned by sales velocity and editable before generating.
3. **Ads Bid Optimizer** (`/ads`) — connect via the Amazon Advertising API, pull
   Sponsored Products keyword metrics, compute new bids from a 4-rule formula, show an
   AI-simplified summary, and apply changes only after you confirm.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in the values
python app.py             # http://localhost:5000
```

### Required configuration (`.env`)

| Key | What it is |
|-----|------------|
| `FERNET_KEY` | Encryption key for stored refresh tokens. Generate: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` |
| `APP_BASE_URL` | Public origin used to build OAuth redirect URIs. **Must be a public https URL (e.g. ngrok)** for real Amazon OAuth; localhost only works for UI testing. |
| `LWA_CLIENT_ID` / `LWA_CLIENT_SECRET` | Your Amazon LWA client (shared by SP-API and Advertising API). |
| `SPAPI_APPLICATION_ID` | The SP-API **App ID** from Seller Central → Develop Apps (looks like `amzn1.sellerapps.app.<uuid>`). **Distinct from the LWA client id** — the consent URL will not work without it. |
| `AI_API_URL` / `AI_API_KEY` | The AI summary endpoint (OpenAI-compatible chat completions assumed). |

### OAuth redirect URIs to register with Amazon
- SP-API app listing **Redirect URI**: `${APP_BASE_URL}/inventory/callback`
- Advertising API allowed return URL: `${APP_BASE_URL}/ads/callback`

The Advertising API must be enabled on the same security profile, and the seller account
onboarded to Advertising, or `/v2/profiles` returns empty.

## Notes
- Modern SP-API requires **no AWS SigV4 signing** — only the LWA access token. The AWS keys
  in `.env` are kept for completeness and are not used for request signing.
- Refresh tokens are encrypted at rest (Fernet) in `data/suite.db` (SQLite, gitignored).
