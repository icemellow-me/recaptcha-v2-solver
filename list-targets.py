#!/usr/bin/env python3
"""List all CDP targets via browser-level WebSocket"""
import json, asyncio, urllib.request

try:
    import websockets
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'websockets', '-q'])
    import websockets

async def main():
    resp = urllib.request.urlopen("http://localhost:9222/json/version")
    info = json.loads(resp.read())
    browser_ws = info['webSocketDebuggerUrl']
    
    async with websockets.connect(browser_ws, max_size=10*1024*1024) as ws:
        await ws.send(json.dumps({'id': 1, 'method': 'Target.getTargets'}))
        resp = json.loads(await ws.recv())
        targets = resp.get('result', {}).get('targetInfos', [])
        
        print(f"Total targets: {len(targets)}")
        for t in targets:
            ttype = t.get('type', '')
            url = t.get('url', '')
            title = t.get('title', '')
            target_id = t.get('targetId', '')
            attached = t.get('attached', False)
            print(f"  type={ttype:20s} attached={str(attached):5s} id={target_id[:24]:24s} title={title[:30]:30s} url={url[:90]}")

asyncio.run(main())
