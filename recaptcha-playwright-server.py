#!/usr/bin/env python3
"""
reCAPTCHA v2 Solver Server (2captcha-compatible API)
Uses Playwright with persistent context to load CaptchaPlugin extension properly.
Then uses CDP to navigate, click checkbox, and extract token.
"""
import asyncio, json, os, sys, time, threading, uuid, urllib.parse, hashlib, signal
from http.server import HTTPServer, BaseHTTPRequestHandler
from playwright.async_api import async_playwright

CDP_PORT = 9333
SERVER_PORT = 8866
EXTENSION_PATH = "/opt/recaptcha-v2-solver/extension"

try:
    with open("/opt/apikey") as f:
        API_KEY = f.read().strip()
except:
    API_KEY = os.environ.get("CAPTCHA_PLUGIN_KEY", "")

# Global browser state
browser = None
browser_ctx = None
browser_page = None
pw_instance = None
cdp_session = None
browser_ready = asyncio.Event()

# Task storage
tasks = {}
tasks_lock = threading.Lock()
solved_count = 0
failed_count = 0
solving_lock = asyncio.Lock()  # Only one solve at a time


class Task:
    def __init__(self, sitekey, pageurl):
        self.id = str(uuid.uuid4().int)[:12]
        self.sitekey = sitekey
        self.pageurl = pageurl
        self.status = "processing"
        self.token = None
        self.error = None
        self.created_at = time.time()


async def init_browser():
    """Launch Playwright with persistent context to load the extension"""
    global browser, browser_ctx, browser_page, pw_instance, cdp_session
    
    print("[browser] Starting Playwright with extension...")
    pw_instance = await async_playwright().start()
    
    # Persistent context is what makes extensions work properly
    # Unlike --load-extension, persistent context registers content scripts BEFORE page load
    user_data_dir = "/tmp/captcha-browser-profile"
    os.makedirs(user_data_dir, exist_ok=True)
    
    # Set DISPLAY for headed mode (needed for extension rendering)
    os.environ.setdefault("DISPLAY", ":99")
    
    browser_ctx = await pw_instance.chromium.launch_persistent_context(
        user_data_dir,
        headless=False,  # Must be false for extensions
        args=[
            f"--disable-extensions-except={EXTENSION_PATH}",
            f"--load-extension={EXTENSION_PATH}",
            "--no-sandbox",
            "--disable-gpu",
            "--window-size=1280,900",
            f"--remote-debugging-port={CDP_PORT}",
        ],
        ignore_default_args=["--disable-extensions"],
    )
    
    # Create a page
    browser_page = await browser_ctx.new_page()
    await browser_page.goto("about:blank")
    
    # Get CDP session for low-level operations
    cdp = await browser_page.context.new_cdp_session(browser_page)
    cdp_session = cdp
    
    # Wait for extension service worker to initialize
    print("[browser] Waiting for extension to initialize...")
    await asyncio.sleep(5)
    
    # Configure extension via CDP - set API key and enable WS mode
    # We need to send messages to the extension's service worker
    # The easiest way: navigate to the extension's options page
    # First, find the extension ID
    pages = browser_ctx.pages
    print(f"[browser] {len(pages)} pages open")
    
    # Try to configure extension via its service worker
    # Navigate to extension options page to set API key
    ext_id = None
    try:
        # List all targets via CDP to find the extension
        result = await cdp.send('Target.getTargets')
        for target in result.get('targetInfos', []):
            url = target.get('url', '')
            if 'chrome-extension://' in url and 'captcha' in url.lower():
                ext_id = url.split('chrome-extension://')[1].split('/')[0]
                break
        
        if ext_id:
            print(f"[browser] Found extension ID: {ext_id}")
            # Navigate to options/popup to configure
            opt_page = await browser_ctx.new_page()
            await opt_page.goto(f"chrome-extension://{ext_id}/popup.html")
            await asyncio.sleep(2)
            
            # Set API key and enable WS mode via the popup
            await opt_page.evaluate(f"""
                () => {{
                    // Try to find and fill in the API key field
                    const inputs = document.querySelectorAll('input');
                    inputs.forEach(i => {{
                        if (i.type === 'text' || i.type === 'password' || i.name?.includes('key') || i.name?.includes('api')) {{
                            i.value = '{API_KEY}';
                            i.dispatchEvent(new Event('input', {{bubbles: true}}));
                        }}
                    }});
                    // Try to click enable/save buttons
                    const buttons = document.querySelectorAll('button');
                    buttons.forEach(b => {{
                        const t = b.textContent.toLowerCase();
                        if (t.includes('save') || t.includes('enable') || t.includes('ws') || t.includes('connect')) {{
                            b.click();
                        }}
                    }});
                }}
            """)
            print("[browser] Configured extension via popup")
            await opt_page.close()
    except Exception as e:
        print(f"[browser] Extension config attempt: {e}")
    
    browser_ready.set()
    print("[browser] Ready!")


async def solve_recaptcha(task):
    """Solve a reCAPTCHA by navigating to the page and clicking the checkbox"""
    global solved_count, failed_count
    
    async with solving_lock:
        try:
            print(f"[task {task.id}] Navigating to {task.pageurl}")
            
            # Navigate the page to the target URL
            await browser_page.goto(task.pageurl, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4)
            
            # Find the reCAPTCHA anchor iframe and click the checkbox
            frames = browser_page.frames
            anchor_frame = None
            for frame in frames:
                url = frame.url
                if 'recaptcha' in url and 'anchor' in url:
                    anchor_frame = frame
                    break
            
            if anchor_frame:
                print(f"[task {task.id}] Found anchor frame, clicking checkbox...")
                # Click the checkbox element inside the anchor iframe
                checkbox = anchor_frame.locator('#recaptcha-anchor')
                await checkbox.click(timeout=10000, force=True)
                print(f"[task {task.id}] Clicked checkbox!")
                # Also dispatch a real mouse event via CDP as backup
                try:
                    box = await checkbox.bounding_box()
                    if box:
                        cx = box['x'] + box['width'] / 2
                        cy = box['y'] + box['height'] / 2
                        cdp = await browser_page.context.new_cdp_session(browser_page)
                        await cdp.send('Input.dispatchMouseEvent', {
                            'type': 'mousePressed', 'x': cx, 'y': cy,
                            'button': 'left', 'clickCount': 1
                        })
                        await asyncio.sleep(0.05)
                        await cdp.send('Input.dispatchMouseEvent', {
                            'type': 'mouseReleased', 'x': cx, 'y': cy,
                            'button': 'left', 'clickCount': 1
                        })
                        print(f"[task {task.id}] CDP mouse click at ({cx:.0f}, {cy:.0f})")
                except Exception as e:
                    print(f"[task {task.id}] CDP click fallback: {e}")
            else:
                print(f"[task {task.id}] No anchor frame found, trying direct click...")
                # Try clicking based on iframe position
                try:
                    iframes = await browser_page.query_selector_all('iframe[src*="recaptcha"]')
                    for iframe in iframes:
                        src = await iframe.get_attribute('src')
                        if src and 'anchor' in src:
                            box = await iframe.bounding_box()
                            if box:
                                cx = box['x'] + 28
                                cy = box['y'] + box['height'] / 2
                                await browser_page.mouse.click(cx, cy)
                                print(f"[task {task.id}] Clicked at ({cx}, {cy})")
                                break
                except Exception as e:
                    print(f"[task {task.id}] Direct click failed: {e}")
            
            # Poll for the token
            start = time.time()
            while time.time() - start < 120:
                try:
                    token = await browser_page.evaluate("""
                        () => {
                            const el = document.getElementById('g-recaptcha-response');
                            if (el && el.value && el.value.length > 10) return el.value;
                            const els = document.querySelectorAll('[name=g-recaptcha-response]');
                            if (els.length && els[0].value && els[0].value.length > 10) return els[0].value;
                            return '';
                        }
                    """)
                    if token:
                        task.token = token
                        task.status = "solved"
                        solved_count += 1
                        print(f"[task {task.id}] SOLVED in {int(time.time()-start)}s")
                        return
                except:
                    pass
                await asyncio.sleep(3)
            
            task.status = "failed"
            task.error = "TIMEOUT"
            failed_count += 1
            print(f"[task {task.id}] TIMEOUT after 120s")
            
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            failed_count += 1
            print(f"[task {task.id}] ERROR: {e}")


def run_solve_in_loop(task):
    """Run async solve in the event loop"""
    asyncio.run_coroutine_threadsafe(solve_recaptcha(task), loop)


# HTTP Handler
class SolverHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[http] {args[0]}")
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        
        if parsed.path == '/health':
            self.send_json({
                "status": "ok",
                "mode": "playwright",
                "queue": len([t for t in tasks.values() if t.status == "processing"]),
                "solved": solved_count,
                "active": len([t for t in tasks.values() if t.status == "processing"]),
                "failed": failed_count,
                "browser": "chromium-playwright"
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
            
            if key != API_KEY:
                self.send_text("ERROR_WRONG_USER_KEY")
                return
            
            googlekey = params.get('googlekey', [''])[0]
            pageurl = params.get('pageurl', [''])[0]
            
            if not googlekey or not pageurl:
                self.send_text("ERROR_MISSING_PARAMS")
                return
            
            task = Task(googlekey, pageurl)
            with tasks_lock:
                tasks[task.id] = task
            
            # Schedule solve in the async loop
            asyncio.run_coroutine_threadsafe(solve_recaptcha(task), loop)
            
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
            
            asyncio.run_coroutine_threadsafe(solve_recaptcha(task), loop)
            
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


async def main():
    global loop
    loop = asyncio.get_event_loop()
    
    print(f"reCAPTCHA v2 Solver Server (Playwright Edition)")
    print(f"  API Key: {API_KEY[:4]}...{API_KEY[-4:]} (len={len(API_KEY)})")
    print(f"  Server Port: {SERVER_PORT}")
    print(f"  Extension: {EXTENSION_PATH}")
    
    # Start browser
    await init_browser()
    
    # Start HTTP server in a thread
    server = HTTPServer(('0.0.0.0', SERVER_PORT), SolverHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"[http] Server listening on :{SERVER_PORT}")
    
    # Keep running
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await pw_instance.stop()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
