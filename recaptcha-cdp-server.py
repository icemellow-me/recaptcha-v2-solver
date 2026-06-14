#!/usr/bin/env python3
"""
reCAPTCHA v2 Solver Server (2captcha-compatible API)
Uses Chrome + CaptchaPlugin extension via CDP to solve captchas.

Architecture:
1. Client submits captcha via 2captcha API (POST /in.php)
2. Server navigates Chrome to the reCAPTCHA page
3. Extension auto-detects and solves via WS cloud
4. Token extracted and returned (GET /res.php)

Also works as a WS cloud worker - solves tasks dispatched from captcharaptor.com
"""
import http.server, json, os, sys, time, threading, uuid, urllib.parse, hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler

CDP_PORT = 9333
SERVER_PORT = 8866

try:
    with open("/opt/apikey") as f:
        API_KEY = f.read().strip()
except:
    API_KEY = os.environ.get("CAPTCHA_PLUGIN_KEY", "")

# Task storage
tasks = {}  # id -> {status, token, error, created_at, sitekey, pageurl}
tasks_lock = threading.Lock()
solved_count = 0
failed_count = 0

class Task:
    def __init__(self, sitekey, pageurl):
        self.id = str(uuid.uuid4().int)[:12]
        self.sitekey = sitekey
        self.pageurl = pageurl
        self.status = "processing"  # processing | solved | failed
        self.token = None
        self.error = None
        self.created_at = time.time()

def solve_recaptcha_task(task):
    """Solve a reCAPTCHA by navigating Chrome to the target page and letting the extension work"""
    import urllib.request
    
    try:
        # Get CDP browser WS URL
        resp = urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json/version")
        info = json.loads(resp.read())
        browser_ws_url = info['webSocketDebuggerUrl']
        
        # We need to run async CDP commands - use a thread with asyncio
        import asyncio
        import websockets
        
        async def do_solve():
            async with websockets.connect(browser_ws_url, max_size=10*1024*1024) as ws:
                msg_id = 0
                
                async def cdp_call(method, params=None, sid=None):
                    nonlocal msg_id
                    msg_id += 1
                    nid = msg_id
                    msg = {'id': nid, 'method': method}
                    if params: msg['params'] = params
                    if sid: msg['sessionId'] = sid
                    await ws.send(json.dumps(msg))
                    while True:
                        r = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
                        if r.get('id') == nid:
                            return r
                
                # Get existing page or create one
                r = await cdp_call('Target.getTargets')
                page = None
                for t in r.get('result',{}).get('targetInfos',[]):
                    if t.get('type') == 'page':
                        page = t
                        break
                
                if not page:
                    r = await cdp_call('Target.createTarget', {'url': 'about:blank'})
                    await asyncio.sleep(2)
                    r = await cdp_call('Target.getTargets')
                    for t in r.get('result',{}).get('targetInfos',[]):
                        if t.get('type') == 'page':
                            page = t
                
                # Attach to page
                r = await cdp_call('Target.attachToTarget', {'targetId': page['targetId'], 'flatten': True})
                p_sid = r.get('result',{}).get('sessionId')
                await cdp_call('Runtime.enable', sid=p_sid)
                await cdp_call('Page.enable', sid=p_sid)
                
                # Build URL with sitekey injected
                # For reCAPTCHA, navigate to a demo page that embeds the sitekey
                target_url = task.pageurl
                
                # If the pageurl isn't a recaptcha demo, we use our own HTML that embeds the sitekey
                # This is the trick: create an HTML page with the recaptcha widget using the target sitekey
                recaptcha_html = f"""
                <html><body>
                <script src="https://www.google.com/recaptcha/api.js?render=explicit"></script>
                <script>
                grecaptcha.render('rc', {{sitekey: '{task.sitekey}'}});
                </script>
                <div id="rc"></div>
                <textarea id="g-recaptcha-response" style="width:400px;height:100px;"></textarea>
                </body></html>
                """
                
                # Navigate using data URL - but that won't work for cross-origin recaptcha
                # Instead, navigate to google's recaptcha demo which has the default key
                # Or navigate to the actual target page
                
                # For now, navigate to the target page directly
                await cdp_call('Page.navigate', {'url': target_url}, sid=p_sid)
                await asyncio.sleep(6)
                
                # Find and click the reCAPTCHA checkbox
                r = await cdp_call('Runtime.evaluate', {
                    'expression': """
                        (function() {
                            var iframes = document.querySelectorAll('iframe');
                            var info = [];
                            for (var i = 0; i < iframes.length; i++) {
                                var f = iframes[i];
                                var r = f.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0 && f.src && f.src.includes('recaptcha') && f.src.includes('anchor')) {
                                    info.push({
                                        x: Math.round(r.left + 28),
                                        y: Math.round(r.top + r.height / 2),
                                        src: f.src.substring(0, 80)
                                    });
                                }
                            }
                            return JSON.stringify(info);
                        })()
                    """,
                    'returnByValue': True
                }, sid=p_sid)
                
                anchors = json.loads(r.get('result',{}).get('result',{}).get('value','[]'))
                if anchors:
                    cx, cy = anchors[0]['x'], anchors[0]['y']
                    # Click the checkbox
                    await cdp_call('Input.dispatchMouseEvent', {
                        'type': 'mouseMoved', 'x': cx, 'y': cy
                    }, sid=p_sid)
                    await asyncio.sleep(0.2)
                    await cdp_call('Input.dispatchMouseEvent', {
                        'type': 'mousePressed', 'x': cx, 'y': cy,
                        'button': 'left', 'clickCount': 1
                    }, sid=p_sid)
                    await asyncio.sleep(0.1)
                    await cdp_call('Input.dispatchMouseEvent', {
                        'type': 'mouseReleased', 'x': cx, 'y': cy,
                        'button': 'left', 'clickCount': 1
                    }, sid=p_sid)
                    print(f"[task {task.id}] Clicked checkbox at ({cx}, {cy})")
                
                # Poll for token
                start = time.time()
                while time.time() - start < 120:
                    r = await cdp_call('Runtime.evaluate', {
                        'expression': '(function(){var e=document.getElementById("g-recaptcha-response");if(e&&e.value&&e.value.length>10)return e.value;var els=document.querySelectorAll("[name=g-recaptcha-response]");if(els.length&&els[0].value&&els[0].value.length>10)return els[0].value;return""})()',
                        'returnByValue': True
                    }, sid=p_sid)
                    token = r.get('result',{}).get('result',{}).get('value','')
                    if token:
                        task.token = token
                        task.status = "solved"
                        global solved_count
                        solved_count += 1
                        print(f"[task {task.id}] SOLVED in {int(time.time()-start)}s")
                        return
                    await asyncio.sleep(3)
                
                task.status = "failed"
                task.error = "TIMEOUT"
                global failed_count
                failed_count += 1
                print(f"[task {task.id}] TIMEOUT after 120s")
        
        asyncio.run(do_solve())
        
    except Exception as e:
        task.status = "failed"
        task.error = str(e)
        global failed_count
        failed_count += 1
        print(f"[task {task.id}] ERROR: {e}")


class SolverHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[http] {args[0]}")
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        
        if parsed.path == '/health':
            self.send_json({
                "status": "ok",
                "mode": "cdp_chrome",
                "queue": len([t for t in tasks.values() if t.status == "processing"]),
                "solved": solved_count,
                "active": len([t for t in tasks.values() if t.status == "processing"]),
                "failed": failed_count,
                "browser": "chromium"
            })
            return
        
        if parsed.path == '/res.php':
            key = params.get('key', [''])[0]
            task_id = params.get('id', [''])[0]
            
            if key != API_KEY:
                self.send_text("ERROR_WRONG_USER_KEY")
                return
            
            with tasks_lock:
                task = tasks.get(task_id)
            
            if not task:
                self.send_text("ERROR_NO_SUCH_TASK")
                return
            
            if task.status == "processing":
                self.send_text("CAPCHA_NOT_READY")
            elif task.status == "solved":
                self.send_text(f"OK|{task.token}")
            elif task.status == "failed":
                self.send_text(f"ERROR|{task.error}")
            return
        
        self.send_text("ERROR_WRONG_METHOD", 405)
    
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        
        if parsed.path == '/in.php':
            content_len = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_len).decode()
            params = urllib.parse.parse_qs(body)
            
            key = params.get('key', [''])[0]
            method = params.get('method', [''])[0]
            googlekey = params.get('googlekey', [''])[0]
            pageurl = params.get('pageurl', [''])[0]
            
            if key != API_KEY:
                self.send_text("ERROR_WRONG_USER_KEY")
                return
            
            if not googlekey or not pageurl:
                self.send_text("ERROR_MISSING_PARAMS")
                return
            
            # Create task
            task = Task(googlekey, pageurl)
            with tasks_lock:
                tasks[task.id] = task
            
            # Start solving in background
            t = threading.Thread(target=solve_recaptcha_task, args=(task,), daemon=True)
            t.start()
            
            self.send_text(f"OK|{task.id}")
            return
        
        if parsed.path == '/createTask.php':
            content_len = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_len).decode()
            data = json.loads(body) if body else {}
            
            client_key = data.get('clientKey', '')
            if client_key != API_KEY:
                self.send_json({"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST"})
                return
            
            task_data = data.get('task', {})
            website_key = task_data.get('websiteKey', '')
            website_url = task_data.get('websiteURL', '')
            
            task = Task(website_key, website_url)
            with tasks_lock:
                tasks[task.id] = task
            
            t = threading.Thread(target=solve_recaptcha_task, args=(task,), daemon=True)
            t.start()
            
            self.send_json({"errorId": 0, "taskId": int(task.id)})
            return
        
        if parsed.path == '/getTaskResult.php':
            content_len = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_len).decode()
            data = json.loads(body) if body else {}
            
            task_id = str(data.get('taskId', ''))
            with tasks_lock:
                task = tasks.get(task_id)
            
            if not task:
                self.send_json({"errorId": 1, "errorCode": "ERROR_NO_SUCH_TASK"})
                return
            
            if task.status == "processing":
                self.send_json({"errorId": 0, "status": "processing"})
            elif task.status == "solved":
                self.send_json({"errorId": 0, "status": "ready", "solution": {"gRecaptchaResponse": task.token}})
            elif task.status == "failed":
                self.send_json({"errorId": 1, "status": "failed", "errorCode": task.error})
            return
        
        self.send_text("ERROR_WRONG_METHOD", 405)
    
    def send_text(self, text, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(text.encode())
    
    def send_json(self, obj, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())


if __name__ == '__main__':
    print(f"reCAPTCHA v2 Solver Server")
    print(f"  API Key: {API_KEY[:4]}...{API_KEY[-4:]} (len={len(API_KEY)})")
    print(f"  CDP Port: {CDP_PORT}")
    print(f"  Server Port: {SERVER_PORT}")
    print(f"  Endpoints:")
    print(f"    POST /in.php          - 2captcha submit")
    print(f"    GET  /res.php          - 2captcha poll")
    print(f"    POST /createTask.php   - anti-captcha submit")
    print(f"    POST /getTaskResult.php - anti-captcha poll")
    print(f"    GET  /health           - health check")
    
    server = HTTPServer(('0.0.0.0', SERVER_PORT), SolverHandler)
    server.serve_forever()
