# reCAPTCHA-v2-Solver

Standalone Python reCAPTCHA v2 solver using Chrome CDP + ONNX AI models.

No browser extension required — runs as a 2captcha-compatible API server.

## How It Works

1. **Chrome CDP Automation** — Controls a headless Chrome instance via DevTools Protocol
2. **ONNX AI Classification** — Uses `type.onnx` (3×3 tile classifier) and `grid.onnx` (4×4 grid classifier) to identify challenge objects
3. **2captcha-compatible API** — `POST /in.php` to submit, `GET /res.php` to poll for tokens

## Prerequisites

- Python 3.11+
- Google Chrome (headless)
- Xvfb (virtual framebuffer)
- ONNX models in `extension/models/`

## Install

```bash
pip install onnxruntime Pillow websockets
apt install google-chrome-stable xvfb
```

## Usage

```bash
# Start Xvfb
Xvfb :100 -screen 0 1920x1080x24 &
export DISPLAY=:100

# Start Chrome with CDP
google-chrome-stable --no-sandbox --disable-gpu --remote-debugging-port=9333 --user-data-dir=/tmp/captcha-chrome about:blank &

# Start the solver server
python3 solver-server.py --api-key YOUR_KEY --port 8866 --cdp-port 9333
```

## API

### Submit a task
```
POST /in.php
key=YOUR_KEY&method=userrecaptcha&googlekey=SITE_KEY&pageurl=PAGE_URL
```
Response: `OK|task_id`

### Get result
```
GET /res.php?key=YOUR_KEY&action=get&id=task_id
```
Response: `OK|recaptcha_token` or `CAPCHA_NOT_READY`

## Architecture

```
solver-server.py        # Main server + CDP automation + ONNX inference
extension/models/
  type.onnx             # 3×3 tile classifier (100×100 → 16 classes)
  grid.onnx             # 4×4 grid classifier (240×240 → 11×16 grid)
  grid.meta.json        # Grid model metadata (thresholds, class mapping)
extension/captcha/
  recaptcha_ai.js       # Original extension AI logic (reference)
```

## Current Status

**Work in progress** — the pipeline works end-to-end (navigate → click → detect → classify → click tiles → verify → extract token) but classification accuracy needs improvement:

- 3×3 challenges: type model classifies individual tiles
- 4×4 challenges: grid model classifies composite image
- Screenshot-based tile capture works but resolution affects accuracy
- Direct composite image download from payload URL is preferred

## TODO

- [ ] Improve image capture quality for better ONNX model accuracy
- [ ] Add perceptual hash (phash) classification as fallback
- [ ] Tune classification thresholds
- [ ] Add retry logic with human-like delays
- [ ] Support reCAPTCHA v3
