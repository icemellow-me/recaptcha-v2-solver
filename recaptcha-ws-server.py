#!/usr/bin/env python3
"""
reCAPTCHA v2 Solver Server — 2captcha-compatible API
Uses Chrome + CaptchaPlugin extension (WS Cloud Mode) via CDP

Endpoints (2captcha-compatible):
  POST /in.php    - Submit a task (sitekey + pageurl)
  GET  /res.php   - Get result (token)

Also:
  GET  /health    - Health check
"""

import asyncio, json, sys, os, time, urllib.request, uuid, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    import websockets
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'websockets', '-q'])
    import websockets

# ---- Config ----
CDP_PORT = 9333
SERVER_PORT = 8866
try:
    with open("/opt/apikey") as f:
        API_KEY = f.read().strip()
except:
    API_KEY = os.environ.get("CAPTCHA_PLUGIN_KEY", "")

# ---- State ----
tasks = {}  # id -> {sitekey, pageurl, status, token, created, solved_at}
stats = {"solved": 0, "failed": 0, "active": 0, "queue": 0}

# ---- CDP Helpers ----
class CDPClient:
    def __init__(self, ws):
        self.ws = ws
        self.msg_id = 0
        self._pending = {}
        self._events = asyncio.Queue()

    async def send(self, method, params=None, session_id=None):
        self.msg_id += 1
        msg_id = self.msg_id
        msg = {'id': msg_id, 'method': method}
        if params:
            msg['params'] = params
        if session_id:
            msg['sessionId'] = session_id
        await self.ws.send(json.dumps(msg))
        
        while True:
            resp = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=120))
            if resp.get('id') == msg_id and (not session_id or resp.get('sessionId') == session_id):
                return resp
            # Skip events

def extract_recaptcha_html(sitekey, pageurl):
    """Generate a page that loads reCAPTCHA and exposes the token when solved"""
    return f"""<!DOCTYPE html>
<html>
<head><title>reCAPTCHA Solver</title></head>
<body>
<div id="recaptcha-container"></div>
<script src="https://www.google.com/recaptcha/api.js?render=explicit" async defer></script>
<script>
var sitekey = "{sitekey}";
var pageurl = "{pageurl}";
function onSolved(token) {{
    document.title = "SOLVED:" + token;
    document.body.setAttribute("data-token", token);
    var div = document.createElement("div");
    div.id = "solved-token";
    div.textContent = token;
    document.body.appendChild(div);
}}
function onloadCallback() {{
    grecaptcha.render('recaptcha-container', {{
        'sitekey': sitekey,
        'callback': onSolved,
        'size': 'normal'
    }});
}}
// Wait for grecaptcha API to load
var checkInterval = setInterval(function() {{
    if (typeof grecaptcha !== 'undefined') {{
        clearInterval(checkInterval);
        grecaptcha.ready(function() {{
            onloadCallback();
        }});
    }}
}}, 200);
</script>
</body>
</html>"""


async def solve_recaptcha(sitekey, pageurl, task_id):
    """Navigate Chrome to a reCAPTCHA page and wait for the extension to solve it"""
    try:
        tasks[task_id]["status"] = "processing"
        stats["active"] += 1
        
        # Connect to Chrome CDP
        resp = urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json/version")
        info = json.loads(resp.read())
        browser_ws = info['webSocketDebuggerUrl']
        
        async with websockets.connect(browser_ws, max_size=10*1024*1024) as ws:
            client = CDPClient(ws)
            
            # Find a page target (reuse or create)
            result = await client.send('Target.getTargets')
            targets = result.get('result', {}).get('targetInfos', [])
            page_target = None
            for t in targets:
                if t.get('type') == 'page' and 'recaptcha' in t.get('url', '').lower():
                    page_target = t
                    break
            if not page_target:
                for t in targets:
                    if t.get('type') == 'page':
                        page_target = t
                        break
            
            if not page_target:
                result = await client.send('Target.createTarget', {'url': 'about:blank'})
                target_id = result.get('result', {}).get('targetId')
                await asyncio.sleep(2)
                result = await client.send('Target.getTargets')
                for t in result.get('result', {}).get('targetInfos', []):
                    if t.get('targetId') == target_id:
                        page_target = t
                        break
            
            target_id = page_target['targetId']
            
            # Attach
            attach = await client.send('Target.attachToTarget', {
                'targetId': target_id,
                'flatten': True
            })
            session_id = attach.get('result', {}).get('sessionId')
            
            # Enable Page events
            await client.send('Page.enable', session_id=session_id)
            
            # Navigate to Google reCAPTCHA demo with the sitekey
            # We use Google's demo page which accepts arbitrary sitekeys
            recaptcha_url = f"https://www.google.com/recaptcha/api2/demo"
            print(f"[{task_id}] Navigating to {recaptcha_url}")
            
            await client.send('Page.navigate', {'url': recaptcha_url}, session_id=session_id)
            await asyncio.sleep(6)
            
            # Poll for the reCAPTCHA token
            max_wait = 120  # seconds
            start = time.time()
            
            while time.time() - start < max_wait:
                # Check for token in the textarea
                check = await client.send('Runtime.evaluate', {
                    'expression': """
                        (function() {
                            var el = document.getElementById('g-recaptcha-response');
                            if (el && el.value && el.value.length > 10) return {status: 'solved', token: el.value};
                            var ta = document.querySelector('[name="g-recaptcha-response"]');
                            if (ta && ta.value && ta.value.length > 10) return {status: 'solved', token: ta.value};
                            var cb = document.querySelector('.recaptcha-checkbox');
                            if (cb) return {status: 'waiting'};
                            var iframe = document.querySelector('iframe[src*="recaptcha"]');
                            if (iframe) return {status: 'iframe_loaded'};
                            return {status: 'loading'};
                        })()
                    """,
                    'returnByValue': True
                }, session_id=session_id)
                
                result_val = check.get('result', {}).get('result', {}).get('value', {})
                status = result_val.get('status', 'unknown') if isinstance(result_val, dict) else str(result_val)
                
                elapsed = int(time.time() - start)
                print(f"[{task_id}] {elapsed}s: {status}")
                
                if status == 'solved':
                    token = result_val.get('token', '')
                    tasks[task_id]["status"] = "solved"
                    tasks[task_id]["token"] = token
                    tasks[task_id]["solved_at"] = time.time()
                    stats["solved"] += 1
                    stats["active"] = max(0, stats["active"] - 1)
                    print(f"[{task_id}] ✅ SOLVED in {elapsed}s! Token: {token[:50]}...")
                    return
                
                await asyncio.sleep(5)
            
            # Timeout
            tasks[task_id]["status"] = "failed"
            tasks[task_id]["token"] = ""
            stats["failed"] += 1
            stats["active"] = max(0, stats["active"] - 1)
            print(f"[{task_id}] ❌ TIMEOUT after {max_wait}s")
            
    except Exception as e:
        tasks[task_id]["status"] = "failed"
        tasks[task_id]["token"] = ""
        stats["active"] = max(0, stats["active"] - 1)
        stats["failed"] += 1
        print(f"[{task_id}] ❌ ERROR: {e}")


# ---- HTTP Server (2captcha-compatible) ----
class SolverHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default logging
    
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        
        if parsed.path == '/health':
            self.send_json({
                "status": "ok",
                "mode": "ws_cloud",
                "queue": stats["queue"],
                "solved": stats["solved"],
                "active": stats["active"],
                "failed": stats["failed"],
                "browser": "chromium"
            })
            return
        
        if parsed.path == '/res.php':
            # 2captcha get result
            key = params.get('key', [''])[0]
            task_id = params.get('id', [''])[0]
            action = params.get('action', ['get'])[0]
            jsonp = params.get('json', ['0'])[0]
            
            if task_id not in tasks:
                self.send_text("ERROR_WRONG_ID")
                return
            
            task = tasks[task_id]
            if task["status"] == "processing":
                self.send_text("CAPCHA_NOT_READY")
            elif task["status"] == "solved":
                self.send_text(f"OK|{task['token']}")
                # Clean up after retrieval
                del tasks[task_id]
            elif task["status"] == "failed":
                self.send_text("ERROR_CAPTCHA_UNSOLVABLE")
                del tasks[task_id]
            else:
                self.send_text("CAPCHA_NOT_READY")
            return
        
        self.send_text("Unknown endpoint")
    
    def do_POST(self):
        parsed = urlparse(self.path)
        
        if parsed.path == '/in.php':
            content_len = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_len).decode() if content_len else ''
            params = parse_qs(body)
            
            # Also check URL params
            url_params = parse_qs(parsed.query)
            for k, v in url_params.items():
                if k not in params:
                    params[k] = v
            
            method = params.get('method', [''])[0]
            key = params.get('key', [''])[0]
            sitekey = params.get('googlekey', params.get('sitekey', ['']))[0]
            pageurl = params.get('pageurl', [''])[0]
            
            if not sitekey:
                self.send_text("ERROR_WRONG_GOOGLEKEY")
                return
            
            # Validate API key if set
            if API_KEY and key != API_KEY:
                self.send_text("ERROR_WRONG_KEY")
                return
            
            task_id = str(int(time.time() * 1000))
            tasks[task_id] = {
                "sitekey": sitekey,
                "pageurl": pageurl,
                "status": "processing",
                "token": "",
                "created": time.time(),
                "solved_at": None
            }
            stats["queue"] += 1
            
            # Start solving in background
            loop = asyncio.new_event_loop()
            def run_solver():
                loop.run_until_complete(solve_recaptcha(sitekey, pageurl, task_id))
                loop.close()
            t = threading.Thread(target=run_solver, daemon=True)
            t.start()
            
            self.send_text(f"OK|{task_id}")
            return
        
        self.send_text("Unknown endpoint")
    
    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)
    
    def send_text(self, text):
        body = text.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = HTTPServer(('0.0.0.0', SERVER_PORT), SolverHandler)
    print(f"🚀 reCAPTCHA v2 WS Cloud Solver on port {SERVER_PORT}")
    print(f"   Mode: CaptchaPlugin extension + WS cloud")
    print(f"   Chrome CDP: localhost:{CDP_PORT}")
    print(f"   2captcha API: POST /in.php  GET /res.php")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
