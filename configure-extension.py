#!/usr/bin/env python3
"""Configure CaptchaPlugin extension - find service worker, set API key, enable WS mode"""
import json, asyncio, sys, urllib.request

try:
    import websockets
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'websockets', '-q'])
    import websockets

CDP_PORT = 9222
API_KEY = "8010000000ccojr5nrbg516w5jvw1wu9"
EXT_ID = None


async def cdp_send(ws, msg_id, method, params=None):
    msg = {'id': msg_id, 'method': method}
    if params:
        msg['params'] = params
    await ws.send(json.dumps(msg))
    while True:
        resp = json.loads(await ws.recv())
        if resp.get('id') == msg_id:
            return resp


async def main():
    global EXT_ID

    resp = urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json/version")
    info = json.loads(resp.read())
    browser_ws = info['webSocketDebuggerUrl']
    print(f"Browser WS: {browser_ws}")

    async with websockets.connect(browser_ws, max_size=10*1024*1024) as ws:
        result = await cdp_send(ws, 1, 'Target.getTargets')
        targets = result.get('result', {}).get('targetInfos', [])

        print(f"\nAll targets ({len(targets)}):")
        ext_sw = None
        for t in targets:
            ttype = t.get('type', '')
            url = t.get('url', '')
            title = t.get('title', '')
            attached = t.get('attached', False)
            print(f"  type={ttype:20s} attached={str(attached):5s} title={title[:40]:40s} url={url[:100]}")

            if ttype == 'service_worker' and 'chrome-extension' in url:
                ext_sw = t

        if not ext_sw:
            print("\nNo extension service worker found. Navigating to trigger it...")
            page_targets = [t for t in targets if t.get('type') == 'page']
            if page_targets:
                page = page_targets[0]
                target_id = page['targetId']

                attach_result = await cdp_send(ws, 10, 'Target.attachToTarget', {
                    'targetId': target_id,
                    'flatten': True
                })
                print(f"Attached to page: {attach_result}")

                nav_result = await cdp_send(ws, 11, 'Page.navigate', {'url': 'https://www.google.com/recaptcha/api2/demo'})
                print(f"Navigate result: {nav_result}")

                await asyncio.sleep(8)

                result2 = await cdp_send(ws, 12, 'Target.getTargets')
                targets2 = result2.get('result', {}).get('targetInfos', [])
                print(f"\nTargets after navigation ({len(targets2)}):")
                for t in targets2:
                    ttype = t.get('type', '')
                    url = t.get('url', '')
                    print(f"  type={ttype:20s} url={url[:120]}")
                    if ttype == 'service_worker' and 'chrome-extension' in url:
                        ext_sw = t

        if ext_sw:
            print(f"\nFound extension service worker!")
            print(f"  Target ID: {ext_sw['targetId']}")
            print(f"  URL: {ext_sw['url']}")

            ext_url = ext_sw['url']
            if 'chrome-extension://' in ext_url:
                EXT_ID = ext_url.split('chrome-extension://')[1].split('/')[0]
                print(f"  Extension ID: {EXT_ID}")

            attach_result = await cdp_send(ws, 20, 'Target.attachToTarget', {
                'targetId': ext_sw['targetId'],
                'flatten': True
            })
            print(f"  Attach result: {attach_result}")

            config_js = (
                "(async () => {"
                "  await chrome.storage.local.set({'reporting': {key: '" + API_KEY + "'}});"
                "  await chrome.storage.local.set({'ws_mode_enabled': true});"
                "  const data = await chrome.storage.local.get(['reporting', 'ws_mode_enabled']);"
                "  return JSON.stringify({"
                "    key_set: data.reporting?.key?.length === 32,"
                "    ws_enabled: data.ws_mode_enabled === true,"
                "    key_preview: data.reporting?.key?.substring(0, 8) + '...'"
                "  });"
                "})()"
            )

            eval_result = await cdp_send(ws, 30, 'Runtime.evaluate', {
                'expression': config_js,
                'awaitPromise': True,
                'returnByValue': True
            })
            print(f"\n  Config result: {eval_result}")
        else:
            print("\nExtension service worker NOT found!")
            print("Checking profile for extension directories...")
            import os
            ext_dir = "/opt/captchaplugin/chrome-profile/Default/Extensions"
            if os.path.exists(ext_dir):
                for d in os.listdir(ext_dir):
                    print(f"  Extension dir: {d}")
            else:
                print("  No Extensions directory found")
                print("  The extension may not be loading. Checking chrome://extensions...")

                # Try navigating to chrome://extensions
                page_targets = [t for t in targets if t.get('type') == 'page']
                if page_targets:
                    page = page_targets[0]
                    target_id = page['targetId']
                    attach_result = await cdp_send(ws, 40, 'Target.attachToTarget', {
                        'targetId': target_id,
                        'flatten': True
                    })
                    nav_result = await cdp_send(ws, 41, 'Page.navigate', {'url': 'chrome://extensions'})
                    await asyncio.sleep(3)
                    eval_result = await cdp_send(ws, 42, 'Runtime.evaluate', {
                        'expression': 'document.body.innerText.substring(0, 1000)',
                        'returnByValue': True
                    })
                    page_text = eval_result.get('result', {}).get('result', {}).get('value', '')
                    print(f"  chrome://extensions content: {page_text[:500]}")


asyncio.run(main())
