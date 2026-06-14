#!/usr/bin/python3
"""
reCAPTCHA v2 Solver Server — Playwright + CaptchaPlugin Extension (WS Cloud Mode)

Uses Playwright persistent context to load the CaptchaPlugin Chrome extension,
which connects to wss://ws.captcharaptor.com for cloud-based image classification.
This bypasses the local ONNX model accuracy issues.

2captcha-compatible API:
  POST /in.php   — submit task (key, method=userrecaptcha, googlekey, pageurl)
  GET  /res.php  — poll result (key, id)
  GET  /health   — health check
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from typing import Dict, Optional

# Configuration
API_KEY = os.environ.get("API_KEY", "")
PORT = int(os.environ.get("PORT", "8866"))
EXTENSION_PATH = "/opt/recaptcha-v2-solver/extension"
PROFILE_DIR = "/tmp/captcha-chrome-profile"

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("recaptcha-ws")

# ---- Task queue ----
pending: Dict[str, dict] = {}
solved: Dict[str, dict] = {}

# ---- Playwright (lazy) ----
pw_browser = None
pw_context = None


async def ensure_browser():
    global pw_browser, pw_context
    if pw_context is not None:
        try:
            # Quick health check — try getting pages
            pages = pw_context.pages
            if pages is not None:
                return pw_context
        except Exception:
            pw_context = None
            pw_browser = None

    from playwright.async_api import async_playwright

    log.info("Launching Playwright persistent context with extension...")
    ap = await async_playwright().start()

    # Clean old profile
    import shutil
    if os.path.exists(PROFILE_DIR):
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    os.makedirs(PROFILE_DIR, exist_ok=True)

    pw_context = await ap.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=False,  # Extensions require headed mode even on Xvfb
        args=[
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            f"--display={os.environ.get('DISPLAY', ':99')}",
        ],
        ignore_default_args=["--disable-extensions"],
        chromium_sandbox=False,
    )
    pw_browser = pw_context

    # Configure extension via CDP — set API key and enable WS mode
    await configure_extension(pw_context)

    log.info("Playwright browser launched with CaptchaPlugin extension")
    return pw_context


async def configure_extension(context):
    """Set API key and WS mode via Chrome storage API through a page"""
    log.info("Configuring CaptchaPlugin extension (API key + WS mode)...")

    # Navigate to a simple page first so we can evaluate JS
    page = await context.new_page()
    await page.goto("about:blank")
    await asyncio.sleep(2)

    # The extension's background.js checks chrome.storage.local for the API key
    # We need to set it through the extension's service worker
    # Unfortunately, we can't directly access chrome.storage from a regular page
    # Instead, we use CDP to attach to the service worker

    cdp = await page.context.new_cdp_session(page)

    # Get all targets to find the extension service worker
    targets = await cdp.send("Target.getTargets")
    sw_target = None
    for t in targets.get("targetInfos", []):
        if t.get("type") == "service_worker" and "chrome-extension" in t.get("url", ""):
            sw_target = t
            break

    if sw_target:
        log.info(f"Found extension service worker: {sw_target['url'][:80]}")
        # Attach and configure
        attach = await cdp.send("Target.attachToTarget", {
            "targetId": sw_target["targetId"],
            "flatten": True,
        })
        session_id = attach.get("sessionId", "")

        config_js = """
        (async () => {
            await chrome.storage.local.set({'reporting': {key: '""" + API_KEY + """'}});
            await chrome.storage.local.set({'ws_mode_enabled': true});
            const data = await chrome.storage.local.get(['reporting', 'ws_mode_enabled']);
            return JSON.stringify({
                key_set: data.reporting?.key?.length === 32,
                ws_enabled: data.ws_mode_enabled === true,
                key_preview: data.reporting?.key?.substring(0, 8) + '...'
            });
        })()
        """
        result = await cdp.send("Runtime.evaluate", {
            "expression": config_js,
            "awaitPromise": True,
            "returnByValue": True,
        })
        log.info(f"Extension config result: {result}")

        # Tell the service worker to connect to WS
        connect_js = """
        (async () => {
            // Trigger WS connection by dispatching alarm
            if (typeof self.wsConnect === 'function') {
                self.wsConnect();
                return 'ws_connect_called';
            }
            return 'no_wsConnect_function';
        })()
        """
        result2 = await cdp.send("Runtime.evaluate", {
            "expression": connect_js,
            "awaitPromise": True,
            "returnByValue": True,
        })
        log.info(f"WS connect result: {result2}")
    else:
        log.warning("No extension service worker found — WS mode may not work")

    await page.close()


async def solve_recaptcha(sitekey: str, pageurl: str) -> Optional[str]:
    """Navigate to a reCAPTCHA page and wait for the extension to solve it via WS"""
    context = await ensure_browser()

    # Open the target page
    page = await context.new_page()
    log.info(f"Navigating to {pageurl}")

    try:
        await page.goto(pageurl, wait_until="networkidle", timeout=30000)
    except Exception as e:
        log.warning(f"Page load timeout (continuing): {e}")

    await asyncio.sleep(2)

    # The extension should auto-detect the reCAPTCHA and start solving via WS
    # We need to wait for the g-recaptcha-response textarea to get a value
    log.info("Waiting for extension to solve reCAPTCHA via WS cloud...")

    for attempt in range(60):  # up to 5 minutes
        try:
            # Check for token in the response textarea
            token = await page.evaluate("""
                () => {
                    // Standard reCAPTCHA response
                    const el = document.getElementById('g-recaptcha-response');
                    if (el && el.value && el.value.length > 10) return el.value;
                    // Sometimes it's in a textarea within the recaptcha div
                    const ta = document.querySelector('.g-recaptcha-response');
                    if (ta && ta.value && ta.value.length > 10) return ta.value;
                    return null;
                }
            """)
            if token and len(token) > 20:
                log.info(f"✅ Token received! Length: {len(token)}")
                await page.close()
                return token
        except Exception as e:
            if "Target closed" in str(e):
                break
            log.debug(f"Token check error: {e}")

        await asyncio.sleep(5)

    # If we didn't get a token, try clicking the checkbox ourselves and waiting
    log.info("No auto-solve detected. Attempting manual click + WS solve...")

    try:
        # Find and click the reCAPTCHA checkbox iframe
        frames = page.frames
        for frame in frames:
            if "recaptcha" in frame.url or "google.com/recaptcha" in frame.url:
                checkbox = await frame.query_selector("#recaptcha-anchor")
                if checkbox:
                    await checkbox.click()
                    log.info("Clicked reCAPTCHA checkbox")
                    break
    except Exception as e:
        log.debug(f"Checkbox click: {e}")

    # Wait again for the solve
    for attempt in range(36):  # up to 3 more minutes
        try:
            token = await page.evaluate("""
                () => {
                    const el = document.getElementById('g-recaptcha-response');
                    if (el && el.value && el.value.length > 10) return el.value;
                    return null;
                }
            """)
            if token and len(token) > 20:
                log.info(f"✅ Token received after click! Length: {len(token)}")
                await page.close()
                return token
        except Exception:
            pass
        await asyncio.sleep(5)

    await page.close()
    return None


# ---- Background solver loop ----
async def solver_loop():
    """Continuously check for pending tasks and solve them"""
    while True:
        for task_id, task in list(pending.items()):
            if task.get("solving"):
                continue
            task["solving"] = True
            log.info(f"Solving task {task_id}: sitekey={task['sitekey']}, pageurl={task['pageurl']}")

            try:
                token = await solve_recaptcha(task["sitekey"], task["pageurl"])
                if token:
                    solved[task_id] = {"token": token, "time": time.time()}
                    del pending[task_id]
                    log.info(f"Task {task_id}: SOLVED")
                else:
                    solved[task_id] = {"error": "ERROR_CAPTCHA_UNSOLVABLE", "time": time.time()}
                    del pending[task_id]
                    log.warning(f"Task {task_id}: UNSOLVABLE")
            except Exception as e:
                solved[task_id] = {"error": f"ERROR_{str(e)[:50]}", "time": time.time()}
                if task_id in pending:
                    del pending[task_id]
                log.error(f"Task {task_id}: Exception {e}")

        await asyncio.sleep(1)


# ---- HTTP handler ----
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(f"HTTP: {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = dict(parse_qs(parsed.query))

        if path == "/health":
            self._json({"status": "ok", "queue": len(pending), "solved": len(solved), "mode": "ws_cloud"})
            return

        if path == "/res.php":
            key = params.get("key", [""])[0]
            if key != API_KEY:
                self._text("ERROR_WRONG_USER_KEY")
                return
            task_id = params.get("id", [""])[0]
            if task_id in solved:
                result = solved.pop(task_id)
                if "token" in result:
                    self._text(f"OK|{result['token']}")
                else:
                    self._text(result.get("error", "ERROR_UNKNOWN"))
            elif task_id in pending:
                self._text("CAPCHA_NOT_READY")
            else:
                self._text("ERROR_NO_SUCH_CAPTCHA_ID")
            return

        self._text("ERROR_INVALID_REQUEST")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/in.php":
            self._text("ERROR_INVALID_REQUEST")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        params = dict(parse_qs(body))

        key = params.get("key", [""])[0]
        if key != API_KEY:
            self._text("ERROR_WRONG_USER_KEY")
            return

        method = params.get("method", [""])[0]
        if method != "userrecaptcha":
            self._text("ERROR_INVALID_METHOD")
            return

        googlekey = params.get("googlekey", [""])[0]
        pageurl = params.get("pageurl", [""])[0]

        if not googlekey or not pageurl:
            self._text("ERROR_MISSING_PARAMETERS")
            return

        task_id = hashlib.md5(f"{googlekey}{pageurl}{time.time()}".encode()).hexdigest()[:12]
        pending[task_id] = {
            "sitekey": googlekey,
            "pageurl": pageurl,
            "time": time.time(),
            "solving": False,
        }
        log.info(f"New task {task_id}: sitekey={googlekey}, pageurl={pageurl}")

        # Start solving in background
        asyncio.ensure_future(self._solve_wrapper(task_id))

        self._text(f"OK|{task_id}")

    async def _solve_wrapper(self, task_id):
        task = pending.get(task_id)
        if not task:
            return
        try:
            token = await solve_recaptcha(task["sitekey"], task["pageurl"])
            if token:
                solved[task_id] = {"token": token, "time": time.time()}
            else:
                solved[task_id] = {"error": "ERROR_CAPTCHA_UNSOLVABLE", "time": time.time()}
        except Exception as e:
            solved[task_id] = {"error": f"ERROR_{str(e)[:50]}", "time": time.time()}
        finally:
            pending.pop(task_id, None)

    def _text(self, msg):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg.encode())

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


# ---- Async HTTP server adapter ----
class AsyncHTTPServer:
    """Wrap HTTPServer to work with asyncio"""
    def __init__(self, port, handler):
        self.server = HTTPServer(("0.0.0.0", port), handler)
        self.server.socket.setblocking(False)

    async def serve(self):
        loop = asyncio.get_event_loop()
        while True:
            try:
                self.server.handle_request()
            except Exception:
                pass
            await asyncio.sleep(0.05)


async def main():
    if not API_KEY:
        print("ERROR: API_KEY env var required")
        sys.exit(1)

    log.info(f"reCAPTCHA WS Cloud Solver starting on port {PORT}")
    log.info(f"API key: {API_KEY[:8]}...")
    log.info(f"Extension path: {EXTENSION_PATH}")

    # Pre-launch browser
    await ensure_browser()

    # Start HTTP server
    http = AsyncHTTPServer(PORT, Handler)

    # Start solver loop
    asyncio.ensure_future(solver_loop())

    log.info(f"Server ready on port {PORT}")

    # Serve
    await http.serve()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--api-key", required=True)
    p.add_argument("--port", type=int, default=8866)
    args = p.parse_args()

    API_KEY = args.api_key
    PORT = args.port

    asyncio.run(main())
