# reCAPTCHA-v2-Solver

Self-hosted reCAPTCHA v2 solver using CaptchaPlugin extension + Playwright.

Provides a **2captcha-compatible API** — drop-in replacement for any 2captcha client.

## Features
- **reCAPTCHA v2** — checkbox + image grid challenges
- **CaptchaPlugin extension** — ONNX AI models for image recognition
- **WebSocket cloud fallback** — dispatches to CaptchaPlugin cloud if local fails
- **Playwright automation** — persistent browser context with extension pre-loaded
- **Anti-detection** — webdriver override, plugin emulation, stealth settings
- **2captcha-compatible API** — POST /in.php, GET /res.php

## Quick Start

```bash
# Install dependencies
pip install playwright aiohttp
playwright install chromium

# Or use install script
bash install-solvers.sh

# Run solver (Playwright + extension mode)
python3 recaptcha-playwright-server.py --port 8866 --api-key YOUR_KEY
```

## API Usage

### Submit reCAPTCHA task
```bash
curl -X POST http://localhost:8866/in.php \
  -d "method=userrecaptcha" \
  -d "key=YOUR_KEY" \
  -d "version=v2" \
  -d "googlekey=6Le-wvkS..." \
  -d "pageurl=https://example.com/page"
```

### Poll result
```bash
curl "http://localhost:8866/res.php?key=YOUR_KEY&id=TASK_ID"
```

### Health check
```bash
curl http://localhost:8866/health
```

## Server Implementations
| File | Mode | Description |
|------|------|-------------|
| `recaptcha-playwright-server.py` | Playwright | Best: extension + stealth, recommended |
| `recaptcha-cdp-server.py` | CDP | Direct Chrome DevTools Protocol |
| `recaptcha-ws-server.py` | WebSocket | CaptchaPlugin cloud dispatch |
| `ws-solver-server.py` | WS | Lightweight WS-only solver |

## Requirements
- Python 3.11+
- Chromium / Chrome
- CaptchaPlugin extension (included in `extension/` directory)
- Xvfb (for headless servers)
- aiohttp, playwright
