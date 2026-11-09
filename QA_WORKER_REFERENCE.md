# RAG Agents Browser QA Worker - Reference Guide

## Overview
Dedicated Cloudflare Worker that controls remote Chromium via Cloudflare Browser Rendering to run automated E2E tests against the production RAG Agents application.

**Worker Name:** `rag-agents-browser-qa`
**URL:** https://rag-agents-browser-qa.pratyakshrawat768.workers.dev
**Account ID:** 01522829f05316893eff23bb4c630deb

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/qa/health` | None | Health check |
| POST | `/qa/run` | Bearer QA_SECRET | Smoke test (page load, auth bypass) |
| POST | `/qa/chat-test` | Bearer QA_SECRET | Full chat E2E test |
| POST | `/qa/call-test` | Bearer QA_SECRET | Voice call feature test |

## Authentication

**Secret Name:** `QA_SECRET`
**Value:** `AI7` (set via `wrangler secret put QA_SECRET`)

```bash
# Usage
curl -X POST https://rag-agents-browser-qa.pratyakshrawat768.workers.dev/qa/run \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer AI7" \
  -d '{"test": "chat"}'
```

## Test Targets

Only approved origin allowed:
- `https://rag-agents.pratyakshrawat768.workers.dev`

## Configuration (wrangler.jsonc)

```jsonc
{
  "name": "rag-agents-browser-qa",
  "main": "src/index.ts",
  "account_id": "01522829f05316893eff23bb4c630deb",
  "compatibility_date": "2026-11-07",
  "compatibility_flags": ["nodejs_compat"],
  "browser": {
    "binding": "BROWSER"
  },
  "vars": {
    "QA_SERVICE_NAME": "rag-agents-browser-qa",
    "DEFAULT_QA_TARGET": "https://rag-agents.pratyakshrawat768.workers.dev/",
    "QA_AUTH_TARGET": "https://rag-agents.pratyakshrawat768.workers.dev"
  }
}
```

## Deployment

```bash
cd /workspace/app/qa-worker
CLOUDFLARE_API_TOKEN=$CLOUDFLARE_API_KEY npx wrangler deploy
```

## Source Code Location
`/workspace/app/qa-worker/src/index.ts` (1591 lines)

## Key Implementation Details

### Browser Launch
Uses `@cloudflare/playwright` with `launch()` binding to Cloudflare Browser Rendering.

### Authentication Flow
1. Navigate to `/auth/qa-login-quick?next=/` on target app
2. Wait for chat view (`.thread` selector)
3. This uses the main worker's QA auth bypass endpoint

### Tests Implemented

**1. Smoke Test (`/qa/run`)**
- Page load verification
- UI element checks (buttons, inputs)
- Screenshot capture

**2. Chat Test (`/qa/chat-test`)**
- Send message via composer
- Wait for AI response
- Verify streaming and completion

**3. Call Test (`/qa/call-test`)**
- Click voice call button (`button[title="Start voice call with Chief"]`)
- Verify call overlay appears (`.call-overlay`, `.call-backdrop`)
- Verify timer (`.call-time`)
- Verify RunBanner/StepCards hidden during call
- End call via end button
- Verify overlay removed

### Selectors Used
```javascript
// Voice call button
"button[title='Start voice call with Chief']"
"button[aria-label='Start voice call with Chief']"

// Call overlay
".call-overlay"
".call-backdrop"
".call-time"

// End call button
"button[title='End call']"
"button[aria-label='End call']"
".call-icbtn[aria-label='End call']"

// Composer
".composer__ta"
"button.composer__send:not(.composer__stop)"
```

### Timeouts
- NAVIGATE_TIMEOUT_MS: 30000
- BROWSER_OPERATION_TIMEOUT_MS: 300000
- AUTH_TIMEOUT_MS: 90000
- RESPONSE_TIMEOUT_MS: 120000

## Common Issues

1. **Rate Limiting** - Browser Rendering has rate limits. Wait 10-15s between calls.
2. **Connection Resets** - `ERR_CONNECTION_RESET` on auth endpoint. Retry after delay.
3. **Browser Creation Failed** - "Rate limit exceeded" from Browser Rendering service.

## Example Test Run

```bash
# Health check
curl https://rag-agents-browser-qa.pratyakshrawat768.workers.dev/qa/health

# Run smoke test
curl -X POST https://rag-agents-browser-qa.pratyakshrawat768.workers.dev/qa/run \
  -H "Authorization: Bearer AI7" \
  -H "Content-Type: application/json" \
  -d '{"test": "chat"}'

# Run call test
curl -X POST https://rag-agents-browser-qa.pratyakshrawat768.workers.dev/qa/call-test \
  -H "Authorization: Bearer AI7" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://rag-agents.pratyakshrawat768.workers.dev/"}'
```

## Local Development

```bash
cd /workspace/app/qa-worker
npm install --legacy-peer-deps
# Note: wrangler dev has compatibility_date issues locally
# Deploy directly for testing
```

## Related Files
- Main worker: `/workspace/app/worker/wrangler.jsonc` (serves frontend from `../Frontend/dist`)
- Frontend source: `/workspace/app/Frontend/src/views/Chat.tsx` (CallOverlay component)
- Frontend styles: `/workspace/app/Frontend/src/styles/desktop-upgrade.css` (call overlay styles)