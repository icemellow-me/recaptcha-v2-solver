#!/usr/bin/python3
"""
Standalone reCAPTCHA v2 solver server.
Uses Chrome (via CDP) + Python ONNX models.
No browser extension required.

API: 2captcha-compatible (POST /in.php, GET /res.php)
     anti-captcha-compatible (POST /createTask.php, POST /getTaskResult.php)
"""

import asyncio, base64, io, json, logging, os, re, sqlite3, sys, time
import traceback, uuid, hashlib
from dataclasses import dataclass, field
from enum import Enum
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread, Lock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
import numpy as np

try:
    import onnxruntime as ort
    HAS_ORT = True
except ImportError:
    HAS_ORT = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False

# ==================== CONFIG ====================
EXT_DIR = "/opt/captchaplugin/extension"
MODELS_DIR = os.path.join(EXT_DIR, "models")
DB_PATH = os.path.join(EXT_DIR, "db", "captcha.sqlite")
CDP_PORT = 9333

TYPE_THRESHOLD = {
    "default": 0.50, "hydrants": 0.014, "bridges": 0.198, "boats": 0.516,
    "cars": 0.25, "crosswalks": 0.303, "taxi": 0.862, "bicycles": 0.041,
    "trafficlights": 0.166, "motorcycles": 0.201, "stairs": 0.16,
    "mountains": 0.598, "tractors": 0.773, "buses": 0.158, "palm": 0.673,
    "parkingmeter": 0.846, "chimney": 0.372,
}

TYPE_INDEX = {
    "boats": 0, "motorcycles": 1, "palm": 2, "parkingmeter": 3, "stairs": 4,
    "taxi": 5, "tractors": 6, "bicycles": 7, "cars": 8, "hydrants": 9,
    "crosswalks": 10, "buses": 11, "trafficlights": 12, "bridges": 13,
    "chimney": 14, "mountains": 15,
}

ALIAS = {
    "fire": "hydrants", "firehydrant": "hydrants", "fire_hydrant": "hydrants",
    "hydrant": "hydrants", "bicycle": "bicycles", "bike": "bicycles",
    "boat": "boats", "bridge": "bridges", "bus": "buses", "car": "cars",
    "chimney": "chimney", "crosswalk": "crosswalks", "zebra": "crosswalks",
    "motorcycle": "motorcycles", "mountain": "mountains", "palm": "palm",
    "parkingmeter": "parkingmeter", "parking": "parkingmeter",
    "stairs": "stairs", "stair": "stairs", "taxi": "taxi", "taxis": "taxi",
    "tractors": "tractors", "tractor": "tractors",
    "traffic": "trafficlights", "trafficlight": "trafficlights",
    "traffic_light": "trafficlights", "trafficlights": "trafficlights",
    "traffic_lights": "trafficlights",
}

KNOWN_LABELS = set(TYPE_INDEX.keys())

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("solver")


# ==================== AI MODELS ====================
class AIModels:
    def __init__(self):
        self.type_sess = None
        self.grid_sess = None
        self.grid_meta = None
        self._load()

    def _load(self):
        if not HAS_ORT:
            log.warning("onnxruntime not available")
            return
        type_path = os.path.join(MODELS_DIR, "type.onnx")
        grid_path = os.path.join(MODELS_DIR, "grid.onnx")
        meta_path = os.path.join(MODELS_DIR, "grid.meta.json")
        if os.path.exists(type_path):
            log.info(f"Loading type model from {type_path}...")
            self.type_sess = ort.InferenceSession(type_path)
            log.info("Type model loaded")
        if os.path.exists(grid_path):
            log.info(f"Loading grid model from {grid_path}...")
            self.grid_sess = ort.InferenceSession(grid_path)
            log.info("Grid model loaded")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                self.grid_meta = json.load(f)
            log.info("Grid meta loaded")

    def img_to_tensor(self, img: Image.Image, size: int) -> np.ndarray:
        img = img.resize((size, size), Image.LANCZOS)
        arr = np.array(img, dtype=np.float32)[:, :, :3]  # ensure RGB
        MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        arr = arr / 255.0
        arr = (arr - MEAN) / STD
        arr = arr.transpose(2, 0, 1)
        return arr.reshape(1, 3, size, size).astype(np.float32)

    def classify_type(self, images: List[Image.Image], label: str) -> List[bool]:
        if not self.type_sess:
            return []
        idx = TYPE_INDEX.get(label)
        if idx is None:
            return []
        thr = TYPE_THRESHOLD.get(label, TYPE_THRESHOLD["default"])
        results = []
        for img in images:
            tensor = self.img_to_tensor(img, 100)
            inp = self.type_sess.get_inputs()[0].name
            output = self.type_sess.run(None, {inp: tensor})
            logits = output[0][0]
            prob = 1.0 / (1.0 + np.exp(-logits[idx]))
            results.append(bool(prob > thr))
            log.info(f"  type: label={label}, prob={prob:.4f}, sel={results[-1]}")
        return results

    def classify_grid(self, img: Image.Image, label: str) -> List[bool]:
        if not self.grid_sess or not self.grid_meta:
            return []
        meta = self.grid_meta
        class_idx = meta.get("type_to_index", {}).get(label)
        thr16 = meta.get("thresholds_by_type", {}).get(label)
        if class_idx is None or not isinstance(thr16, list) or len(thr16) != 16:
            return []
        grid_size = meta.get("image_size", 240)
        tensor = self.img_to_tensor(img, grid_size)
        inp = self.grid_sess.get_inputs()[0].name
        output = self.grid_sess.run(None, {inp: tensor})
        # Output shape: (1, num_classes, 16) — logits per class per cell
        logits = output[0]  # shape: (num_classes, 16) or (1, num_classes, 16)
        if logits.ndim == 3:
            logits = logits[0]  # squeeze batch dim → (num_classes, 16)
        class_logits = logits[class_idx]  # shape: (16,)
        probs = [float(1.0 / (1.0 + np.exp(-v))) for v in class_logits]
        data = [probs[i] > (thr16[i] if i < len(thr16) else 0.5) for i in range(len(probs))]
        log.info(f"  grid: label={label}, class_idx={class_idx}, probs={[f'{p:.3f}' for p in probs]}, sel={data}")
        return data

    def classify_tiles_for_grid(self, tiles: List[Image.Image], label: str, grid_type: str) -> List[bool]:
        """Classify individual tile images for grid challenges (4x4, 4x2).
        Stitches tiles back together and uses the grid model."""
        if not self.grid_sess or not self.grid_meta:
            # Fallback to type model
            return self.classify_type(tiles, label)
        # Stitch tiles into a composite image
        if grid_type == "4x4" and len(tiles) == 16:
            cols, rows = 4, 4
        elif grid_type == "4x2" and len(tiles) == 8:
            cols, rows = 4, 2
        else:
            # Unknown grid, fallback to type model
            return self.classify_type(tiles, label)
        
        tw = tiles[0].size[0] if tiles else 100
        th = tiles[0].size[1] if tiles else 100
        composite = Image.new("RGB", (tw * cols, th * rows))
        for i, tile in enumerate(tiles):
            r, c = divmod(i, cols)
            composite.paste(tile.resize((tw, th)), (c * tw, r * th))
        
        return self.classify_grid(composite, label)


# ==================== CDP HELPERS ====================
async def cdp_connect(port=CDP_PORT):
    """Connect to browser CDP endpoint."""
    import urllib.request
    resp = urllib.request.urlopen(f"http://localhost:{port}/json/version").read()
    info = json.loads(resp)
    ws_url = info["webSocketDebuggerUrl"]
    ws = await websockets.connect(ws_url, max_size=50*1024*1024)
    return ws


async def cdp_send(ws, method, params=None, _id=[0]):
    """Send CDP command and wait for response, ignoring events."""
    _id[0] += 1
    mid = _id[0]
    msg = json.dumps({"id": mid, "method": method, "params": params or {}})
    await ws.send(msg)
    deadline = time.time() + 30
    while time.time() < deadline:
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time())))
        if resp.get("id") == mid:
            if "error" in resp:
                raise Exception(f"CDP error: {resp['error']}")
            return resp.get("result", {})
        # Ignore events and other message responses
    raise Exception(f"CDP timeout waiting for response to {method}")


async def cdp_listen(ws, event_method, timeout=30):
    """Listen for a specific CDP event."""
    while True:
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if resp.get("method") == event_method:
            return resp.get("params", {})


async def page_connect(port, target_id):
    """Connect to a specific page target."""
    import urllib.request
    resp = urllib.request.urlopen(f"http://localhost:{port}/json").read()
    targets = json.loads(resp)
    for t in targets:
        if t.get("id") == target_id:
            return await websockets.connect(t["webSocketDebuggerUrl"], max_size=50*1024*1024)
    raise Exception(f"Target {target_id} not found")


# ==================== RECAPTCHA SOLVER ====================
class TaskStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SOLVED = "solved"
    FAILED = "failed"


@dataclass
class SolveTask:
    task_id: str
    sitekey: str
    pageurl: str
    status: TaskStatus = TaskStatus.PENDING
    token: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    solved_at: Optional[float] = None


class ReCaptchaSolver:
    def __init__(self, cdp_port=CDP_PORT):
        self.cdp_port = cdp_port
        self.ai = AIModels()
        self.tasks: Dict[str, SolveTask] = {}
        self.lock = Lock()
        self._solver_task = None

    async def start(self):
        self._solver_task = asyncio.create_task(self._solver_loop())
        log.info("Solver loop started")

    async def stop(self):
        if self._solver_task:
            self._solver_task.cancel()

    def create_task(self, sitekey, pageurl) -> str:
        task_id = uuid.uuid4().hex[:12]
        with self.lock:
            self.tasks[task_id] = SolveTask(task_id=task_id, sitekey=sitekey, pageurl=pageurl)
        return task_id

    def get_task(self, task_id) -> Optional[SolveTask]:
        with self.lock:
            return self.tasks.get(task_id)

    async def _solver_loop(self):
        while True:
            try:
                pending = None
                with self.lock:
                    for task in self.tasks.values():
                        if task.status == TaskStatus.PENDING:
                            pending = task
                            break
                if pending:
                    pending.status = TaskStatus.PROCESSING
                    try:
                        token = await self._solve(pending.sitekey, pending.pageurl)
                        pending.token = token
                        pending.status = TaskStatus.SOLVED
                        pending.solved_at = time.time()
                        log.info(f"Task {pending.task_id} SOLVED (token len={len(token)})")
                    except Exception as e:
                        pending.status = TaskStatus.FAILED
                        pending.error = str(e)
                        log.error(f"Task {pending.task_id} FAILED: {e}")
                        traceback.print_exc()
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Solver loop error: {e}")
                await asyncio.sleep(5)

    def _extract_label(self, text):
        """Extract the target label from challenge text like 'Select all images with traffic lights'."""
        text = text.lower().strip()
        # Try multiple patterns, from most specific to least
        patterns = [
            r'select\s+all\s+images\s+with\s+(?:an?\s+)?([a-z]+(?:\s+[a-z]+)?)\s*(?:click|$)',
            r'select\s+all\s+images\s+with\s+(?:an?\s+)?([a-z]+(?:\s+[a-z]+)?)\s*\.?',
            r'select\s+all\s+(?:images\s+)?with\s+(?:an?\s+)?([a-z]+(?:\s+[a-z]+)?)',
            r'images\s+with\s+(?:an?\s+)?([a-z]+(?:\s+[a-z]+)?)',
            r'find\s+(?:the\s+)?(?:an?\s+)?([a-z]+(?:\s+[a-z]+)?)',
            r'with\s+(?:an?\s+)?([a-z]+)',
        ]
        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                label = m.group(1).strip()
                # Remove trailing words that aren't part of the label
                stop_words = ['click', 'verify', 'once', 'there', 'are', 'none', 'left', 'if', 'no', 'match']
                words = label.split()
                while words and words[-1] in stop_words:
                    words.pop()
                if words:
                    return ' '.join(words)
        # Fallback: last word before "click" or end
        m = re.search(r'with\s+(?:an?\s+)?(\w+)', text)
        if m:
            return m.group(1)
        return text.split()[-1] if text.split() else ""

    def _normalize_label(self, raw):
        if not raw:
            return None
        raw = raw.strip().lower().replace(" ", "_")
        if raw in ALIAS:
            return ALIAS[raw]
        if raw in KNOWN_LABELS:
            return raw
        if not raw.endswith("s"):
            plural = raw + "s"
            if plural in KNOWN_LABELS:
                return plural
            if plural in ALIAS:
                return ALIAS[plural]
        return None

    async def _solve(self, sitekey, pageurl) -> str:
        """Solve a reCAPTCHA v2 challenge using CDP + ONNX models."""
        log.info(f"Solving: sitekey={sitekey}, pageurl={pageurl}")

        # Connect to browser
        browser_ws = await cdp_connect(self.cdp_port)

        # Create new tab
        result = await cdp_send(browser_ws, "Target.createTarget", {"url": "about:blank"})
        target_id = result["targetId"]
        log.info(f"Created tab: {target_id}")

        # Connect to the new page
        # Get page WS
        await asyncio.sleep(1)
        page_ws = await page_connect(self.cdp_port, target_id)
        pid = [0]  # message id counter
            
        try:
            # Enable domains
            await cdp_send(page_ws, "Page.enable", _id=pid)
            await cdp_send(page_ws, "Runtime.enable", _id=pid)

            # Navigate to the page with reCAPTCHA
            log.info(f"Navigating to {pageurl}")
            await cdp_send(page_ws, "Page.navigate", {"url": pageurl}, _id=pid)
            await asyncio.sleep(6)

            # ===== STEP 1: Click the reCAPTCHA checkbox =====
            log.info("Step 1: Finding and clicking reCAPTCHA checkbox...")

            # Find the anchor iframe position
            iframe_js = """
                (function() {
                    var iframes = document.querySelectorAll('iframe[src*="recaptcha"]');
                    for (var i = 0; i < iframes.length; i++) {
                        if (iframes[i].src.indexOf('anchor') !== -1) {
                            var r = iframes[i].getBoundingClientRect();
                            return JSON.stringify({x: r.x, y: r.y, w: r.width, h: r.height, found: true});
                        }
                    }
                    // Fallback: any recaptcha iframe
                    var any = document.querySelector('iframe[src*="recaptcha"]');
                    if (any) {
                        var r = any.getBoundingClientRect();
                        return JSON.stringify({x: r.x, y: r.y, w: r.width, h: r.height, found: true, fallback: true});
                    }
                    return JSON.stringify({found: false});
                })()
            """
            pos_result = await cdp_send(page_ws, "Runtime.evaluate", {
                "expression": iframe_js, "returnByValue": True
            }, _id=pid)
            pos = json.loads(pos_result.get("result", {}).get("value", "{}"))
            log.info(f"Anchor iframe: {pos}")

            if pos.get("found"):
                # Click checkbox (left-center of the anchor iframe)
                cx = pos["x"] + 28
                cy = pos["y"] + pos["h"] / 2
                for evt in ["mousePressed", "mouseReleased"]:
                    await cdp_send(page_ws, "Input.dispatchMouseEvent", {
                        "type": evt, "x": cx, "y": cy,
                        "button": "left", "clickCount": 1
                    }, _id=pid)
                log.info(f"Clicked checkbox at ({cx}, {cy})")

            # Wait for challenge or auto-solve
            await asyncio.sleep(5)

            # Check if already solved (low-risk checkbox)
            token = await self._get_token(page_ws, pid)
            if token:
                log.info("Auto-solved (no challenge needed)")
                return token

            # ===== STEP 2: Solve the image challenge =====
            for round_num in range(10):
                log.info(f"Step 2: Challenge round {round_num + 1}")

                # Find bframe iframe
                tree = await cdp_send(page_ws, "Page.getFrameTree", _id=pid)
                frames = self._collect_frames(tree.get("frameTree", {}))

                bframe_id = None
                for f in frames:
                    if "bframe" in f.get("url", ""):
                        bframe_id = f.get("id") or f.get("frameId")
                        break

                if not bframe_id:
                    log.warning("No bframe found")
                    token = await self._get_token(page_ws, pid)
                    if token:
                        return token
                    await asyncio.sleep(3)
                    continue

                # Create execution context in bframe
                ctx_result = await cdp_send(page_ws, "Page.createIsolatedWorld", {
                    "frameId": bframe_id, "grantUniveralAccess": True
                }, _id=pid)
                ctx_id = ctx_result.get("executionContextId")

                if not ctx_id:
                    raise Exception("Could not create bframe context")

                # Get task info
                info_js = """
                    (function() {
                        var desc = document.querySelector('.rc-imageselect-desc');
                        var descNoCh = document.querySelector('.rc-imageselect-desc-no-canonical');
                        var txt = (desc || descNoCh || document.querySelector('.rc-imageselect-instructions')).textContent.trim();
                        
                        // Clean up: remove instructional text that follows the task
                        txt = txt.replace(/Click verify.*$/i, '').trim();
                        txt = txt.replace(/If there are none.*$/i, '').trim();
                        txt = txt.replace(/If none.*$/i, '').trim();
                        txt = txt.replace(/Click skip.*$/i, '').trim();
                        
                        var tbl = document.querySelector('table');
                        var cls = tbl ? tbl.className : '';
                        var grid = '3x3';
                        if (cls.indexOf('table-44') !== -1) grid = '4x4';
                        else if (cls.indexOf('table-42') !== -1) grid = '4x2';
                        var tiles = document.querySelectorAll('td.rc-imageselect-tile');
                        return JSON.stringify({task: txt, grid: grid, numTiles: tiles.length});
                    })()
                """
                info_result = await cdp_send(page_ws, "Runtime.evaluate", {
                    "expression": info_js, "contextId": ctx_id, "returnByValue": True
                }, _id=pid)
                info = json.loads(info_result.get("result", {}).get("value", "{}"))
                log.info(f"Challenge: {info}")

                if not info.get("task"):
                    token = await self._get_token(page_ws, pid)
                    if token:
                        return token
                    await asyncio.sleep(3)
                    continue

                raw_label = self._extract_label(info["task"])
                label = self._normalize_label(raw_label)
                grid_type = info.get("grid", "3x3")
                log.info(f"Label: '{raw_label}' -> '{label}', grid: {grid_type}")

                if not label:
                    raise Exception(f"Unsupported label: {raw_label}")

                # Get challenge tiles via screenshot + crop approach
                # Canvas drawImage doesn't work for 3x3 because all tiles share one composite image
                # CSS clips the display; canvas doesn't replicate that
                log.info("Capturing tile images via screenshot + crop...")
                
                # First, try to get the composite image URL from bframe
                src_js = """
                    (function() {
                        var img = document.querySelector('.rc-imageselect-tile img, .rc-imageselect-image-wrapper img, .rc-image-tile-44 img');
                        if (img && img.src) return JSON.stringify({src: img.src, nw: img.naturalWidth, nh: img.naturalHeight});
                        // Try background-image
                        var wrapper = document.querySelector('.rc-imageselect-image-wrapper');
                        if (wrapper) {
                            var bg = getComputedStyle(wrapper).backgroundImage;
                            var m = bg.match(/url\\(["']?([^"')]+)/);
                            if (m) return JSON.stringify({src: m[1], nw: 0, nh: 0});
                        }
                        return '{}';
                    })()
                """
                src_result = await cdp_send(page_ws, "Runtime.evaluate", {
                    "expression": src_js, "contextId": ctx_id, "returnByValue": True
                }, _id=pid)
                src_info = json.loads(src_result.get("result", {}).get("value", "{}"))
                
                tile_images = []
                challenge_img = None
                
                if src_info.get("src") and src_info["src"].startswith("http"):
                    # Download the composite image directly
                    import urllib.request
                    try:
                        log.info(f"Downloading composite image from payload URL...")
                        resp = urllib.request.urlopen(src_info["src"])
                        challenge_img = Image.open(io.BytesIO(resp.read())).convert("RGB")
                        log.info(f"Downloaded composite: {challenge_img.size}")
                        
                        # Split composite into tiles
                        if grid_type == "4x4":
                            w, h = challenge_img.size
                            tw, th = w // 4, h // 4
                            tile_images = [challenge_img.crop((c*tw, r*th, (c+1)*tw, (r+1)*th)) 
                                          for r in range(4) for c in range(4)]
                        elif grid_type == "3x3":
                            w, h = challenge_img.size
                            tw, th = w // 3, h // 3
                            tile_images = [challenge_img.crop((c*tw, r*th, (c+1)*tw, (r+1)*th)) 
                                          for r in range(3) for c in range(3)]
                        else:
                            tile_images = [challenge_img]
                    except Exception as e:
                        log.warning(f"Failed to download composite: {e}")
                
                # Fallback: screenshot + crop
                if not tile_images:
                    log.info("Falling back to screenshot + crop...")
                    
                    # Get bframe position on page
                    bframe_js = """
                        (function() {
                            var f = document.querySelector('iframe[src*="bframe"]');
                            if (!f) return '{}';
                            var r = f.getBoundingClientRect();
                            return JSON.stringify({x:r.x, y:r.y, w:r.width, h:r.height});
                        })()
                    """
                    bf_result = await cdp_send(page_ws, "Runtime.evaluate", {
                        "expression": bframe_js, "returnByValue": True
                    }, _id=pid)
                    bf_pos = json.loads(bf_result.get("result", {}).get("value", "{}"))
                    
                    if bf_pos.get("x") is None:
                        raise Exception("Could not find bframe position")
                    
                    # Get tile positions within bframe
                    tile_js = """
                        (function() {
                            var tiles = document.querySelectorAll('td.rc-imageselect-tile');
                            var pos = [];
                            tiles.forEach(function(t) {
                                var r = t.getBoundingClientRect();
                                pos.push({x: Math.round(r.x), y: Math.round(r.y), 
                                          w: Math.round(r.width), h: Math.round(r.height)});
                            });
                            return JSON.stringify(pos);
                        })()
                    """
                    tile_result = await cdp_send(page_ws, "Runtime.evaluate", {
                        "expression": tile_js, "contextId": ctx_id, "returnByValue": True
                    }, _id=pid)
                    tile_positions = json.loads(tile_result.get("result", {}).get("value", "[]"))
                    log.info(f"Found {len(tile_positions)} tiles in bframe")
                    
                    # Take page screenshot at 2x scale for better quality
                    screenshot = await cdp_send(page_ws, "Page.captureScreenshot", {
                        "format": "png", "scale": 2
                    }, _id=pid)
                    if not screenshot.get("data"):
                        raise Exception("Screenshot capture failed")
                    
                    page_img = Image.open(io.BytesIO(base64.b64decode(screenshot["data"])))
                    dpr = screenshot.get("deviceScaleFactor", 1) * 2  # viewport DPR * screenshot scale
                    
                    # Crop each tile from the screenshot
                    tile_images = []
                    for tp in tile_positions:
                        tx = (bf_pos["x"] + tp["x"]) * dpr
                        ty = (bf_pos["y"] + tp["y"]) * dpr
                        tw = tp["w"] * dpr
                        th = tp["h"] * dpr
                        crop = page_img.crop((int(tx), int(ty), int(tx + tw), int(ty + th)))
                        tile_images.append(crop)
                    log.info(f"Cropped {len(tile_images)} tile images ({tile_images[0].size if tile_images else 'none'})")

                if not tile_images:
                    raise Exception("Could not get tile images")

                # Classify the tiles
                if grid_type in ("4x4", "4x2") and challenge_img:
                    # Use grid model directly on the composite image (best quality)
                    selection = self.ai.classify_grid(challenge_img, label)
                elif grid_type == "3x3":
                    selection = self.ai.classify_type(tile_images, label)
                elif grid_type in ("4x4", "4x2"):
                    selection = self.ai.classify_tiles_for_grid(tile_images, label, grid_type)
                else:
                    selection = self.ai.classify_type(tile_images, label)

                if not selection:
                    raise Exception("AI classification returned empty")

                log.info(f"Selection: {selection}")

                # Click selected tiles
                for idx, selected in enumerate(selection):
                    if not selected:
                        continue
                    click_js = f"""
                        (function() {{
                            var tiles = document.querySelectorAll('td.rc-imageselect-tile');
                            if (tiles[{idx}]) {{
                                tiles[{idx}].click();
                                return 'clicked';
                            }}
                            return 'not_found';
                        }})()
                    """
                    await cdp_send(page_ws, "Runtime.evaluate", {
                        "expression": click_js, "contextId": ctx_id, "returnByValue": True
                    }, _id=pid)
                    await asyncio.sleep(0.3)

                # Click verify
                await asyncio.sleep(1)
                verify_js = """
                    (function() {
                        var btn = document.querySelector('#recaptcha-verify-button');
                        if (btn) { btn.click(); return 'clicked'; }
                        return 'not_found';
                    })()
                """
                await cdp_send(page_ws, "Runtime.evaluate", {
                    "expression": verify_js, "contextId": ctx_id, "returnByValue": True
                }, _id=pid)
                log.info("Clicked verify button")

                # Wait and check for token
                await asyncio.sleep(5)
                token = await self._get_token(page_ws, pid)
                if token:
                    return token

                log.info("No token yet, next round...")

            raise Exception("Failed to solve after 10 rounds")

        finally:
            # Cleanup
            try:
                await page_ws.close()
            except Exception:
                pass
            try:
                await cdp_send(browser_ws, "Target.closeTarget", {"targetId": target_id})
            except Exception:
                pass
            try:
                await browser_ws.close()
            except Exception:
                pass

    async def _get_token(self, ws, pid) -> Optional[str]:
        """Check if reCAPTCHA token is available."""
        result = await cdp_send(ws, "Runtime.evaluate", {
            "expression": 'document.getElementById("g-recaptcha-response")?.value || ""',
            "returnByValue": True
        }, _id=pid)
        token = result.get("result", {}).get("value", "")
        if token and len(token) > 10:
            return token
        return None

    def _collect_frames(self, tree, frames=None):
        if frames is None:
            frames = []
        if isinstance(tree, dict):
            frame = tree.get("frame")
            if frame:
                frames.append(frame)
            for child in tree.get("childFrames", []):
                self._collect_frames(child, frames)
        return frames


# ==================== HTTP API ====================
class Handler(BaseHTTPRequestHandler):
    solver: ReCaptchaSolver = None
    api_key: str = ""

    def log_message(self, fmt, *args):
        log.debug(f"HTTP: {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/res.php":
            self._get_result(params)
        elif parsed.path == "/health":
            self._health()
        elif parsed.path == "/getTaskResult.php":
            self._get_task_result(params)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/in.php":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            self._submit(parse_qs(body))
        elif parsed.path == "/createTask.php":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._create_task(body)
        else:
            self.send_error(404)

    def _check_key(self, params):
        return params.get("key", [""])[0] == self.api_key

    def _submit(self, params):
        if not self._check_key(params):
            return self._text("ERROR_WRONG_USER_KEY")
        if params.get("method", [""])[0] != "userrecaptcha":
            return self._text("ERROR_WRONG_METHOD")
        gk = params.get("googlekey", [""])[0]
        pu = params.get("pageurl", [""])[0]
        if not gk or not pu:
            return self._text("ERROR_MISSING_FIELDS")
        tid = self.solver.create_task(gk, pu)
        if params.get("json", ["0"])[0] == "1":
            self._json({"status": 1, "request": tid})
        else:
            self._text(f"OK|{tid}")

    def _get_result(self, params):
        if not self._check_key(params):
            return self._text("ERROR_WRONG_USER_KEY")
        tid = params.get("id", [""])[0]
        task = self.solver.get_task(tid)
        if not task:
            return self._text("ERROR_CAPTCHA_UNSOLVABLE")
        if task.status == TaskStatus.SOLVED:
            if params.get("json", ["0"])[0] == "1":
                self._json({"status": 1, "request": task.token})
            else:
                self._text(f"OK|{task.token}")
        elif task.status == TaskStatus.FAILED:
            self._text("ERROR_CAPTCHA_UNSOLVABLE")
        else:
            self._text("CAPCHA_NOT_READY")

    def _create_task(self, body):
        try:
            data = json.loads(body)
        except Exception:
            return self._json({"errorId": 1, "errorCode": "ERROR_INVALID_JSON"})
        if data.get("clientKey") != self.api_key:
            return self._json({"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST"})
        task = data.get("task", {})
        if task.get("type", "") not in ("RecaptchaV2TaskProxyless", "NoCaptchaTaskProxyless"):
            return self._json({"errorId": 1, "errorCode": "ERROR_WRONG_TASK_TYPE"})
        wk = task.get("websiteKey", "")
        wu = task.get("websiteURL", "")
        if not wk or not wu:
            return self._json({"errorId": 1, "errorCode": "ERROR_MISSING_FIELDS"})
        tid = self.solver.create_task(wk, wu)
        self._json({"errorId": 0, "taskId": tid})

    def _get_task_result(self, params):
        # Anti-captcha uses POST, but handle GET too
        tid = params.get("taskId", [""])[0]
        task = self.solver.get_task(tid)
        if not task:
            return self._json({"errorId": 1, "errorCode": "ERROR_TASK_NOT_FOUND"})
        if task.status == TaskStatus.SOLVED:
            self._json({"errorId": 0, "status": "ready", "solution": {"gRecaptchaResponse": task.token}})
        elif task.status == TaskStatus.FAILED:
            self._json({"errorId": 1, "errorCode": "ERROR_CAPTCHA_UNSOLVABLE"})
        else:
            self._json({"errorId": 0, "status": "processing"})

    def _health(self):
        q = sum(1 for t in self.solver.tasks.values() if t.status in (TaskStatus.PENDING, TaskStatus.PROCESSING))
        s = sum(1 for t in self.solver.tasks.values() if t.status == TaskStatus.SOLVED)
        self._json({"status": "ok", "queue": q, "solved": s, "models": self.solver.ai.type_sess is not None})

    def _text(self, s):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(s.encode())

    def _json(self, d):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(d).encode())


# ==================== MAIN ====================
async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--port", type=int, default=8866)
    parser.add_argument("--cdp-port", type=int, default=CDP_PORT)
    args = parser.parse_args()

    solver = ReCaptchaSolver(cdp_port=args.cdp_port)
    await solver.start()

    Handler.solver = solver
    Handler.api_key = args.api_key

    server = HTTPServer(("0.0.0.0", args.port), Handler)
    Thread(target=server.serve_forever, daemon=True).start()

    log.info(f"Solver API on port {args.port}")
    log.info(f"API key: {args.api_key[:8]}...")
    log.info(f"CDP port: {args.cdp_port}")
    log.info(f"ONNX: type={solver.ai.type_sess is not None}, grid={solver.ai.grid_sess is not None}")

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        await solver.stop()
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
