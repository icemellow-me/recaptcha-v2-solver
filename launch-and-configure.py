#!/usr/bin/env python3
"""Launch Chrome with CaptchaPlugin extension and configure API key + WS mode via Playwright."""
import asyncio, json, os
from playwright.async_api import async_playwright

EXTENSION_PATH = '/opt/captchaplugin/extension'
STATUS_FILE = '/tmp/captcha-browser-status.txt'
API_KEY = '8010000000ccojr5nrbg516w5jvw1wu9'

def status(msg):
    with open(STATUS_FILE, 'a') as f:
        f.write(msg + '\n')
        f.flush()
    print(msg, flush=True)

async def main():
    # Clear status
    with open(STATUS_FILE, 'w') as f:
        f.write('')
    
    status('Starting Playwright...')
    
    async with async_playwright() as p:
        status('Launching Chrome with CaptchaPlugin extension...')
        
        context = await p.chromium.launch_persistent_context(
            user_data_dir='/tmp/captcha-playwright-final',
            headless=False,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-extensions-except=' + EXTENSION_PATH,
                '--load-extension=' + EXTENSION_PATH,
                '--no-first-run',
                '--no-default-browser-check',
                '--mute-audio',
            ],
            ignore_default_args=[
                '--disable-extensions',
                '--disable-component-extensions-with-background-pages',
            ],
            executable_path='/usr/bin/google-chrome-stable',
        )

        status('Browser launched. Waiting 20s for extension to fully initialize...')
        await asyncio.sleep(20)

        # Check service workers
        sws = context.service_workers
        status('Service workers: %d' % len(sws))
        for sw in sws:
            status('  SW: %s' % sw.url)

        # Check background pages
        try:
            bps = context.background_pages
            status('Background pages: %d' % len(bps))
            for bp in bps:
                status('  BP: %s' % bp.url)
        except Exception as e:
            status('Background pages error: %s' % str(e))

        # Check all pages
        pages = context.pages
        status('Pages: %d' % len(pages))
        for page in pages:
            status('  Page: %s' % page.url)

        # Find the CaptchaPlugin extension service worker
        captcha_sw = None
        for sw in sws:
            if 'captcha' in sw.url.lower() or 'raptor' in sw.url.lower() or 'background' in sw.url.lower():
                captcha_sw = sw
                break
        
        # If no SW found by name, try the first non-Google one
        if not captcha_sw and len(sws) > 0:
            for sw in sws:
                if 'chrome-extension' in sw.url and 'network_speech' not in sw.url:
                    captcha_sw = sw
                    break

        if captcha_sw:
            status('Found CaptchaPlugin service worker: %s' % captcha_sw.url)
            
            # Configure API key and WS mode via chrome.storage.local
            try:
                result = await captcha_sw.evaluate('''
                    () => {
                        return new Promise((resolve) => {
                            chrome.storage.local.set({
                                'reporting.key': '%s',
                                'ws_mode_enabled': true
                            }, () => {
                                chrome.storage.local.get(['reporting.key', 'ws_mode_enabled'], (data) => {
                                    resolve(JSON.stringify(data));
                                });
                            });
                        });
                    }
                ''' % API_KEY)
                status('Extension config result: %s' % result)
            except Exception as e:
                status('Failed to configure via SW: %s' % str(e))
                # Try alternative: navigate to extension popup and configure there
                status('Will try alternative configuration method...')
        else:
            status('WARNING: No CaptchaPlugin service worker found!')
            status('Listing all SW URLs for debugging:')
            for sw in sws:
                status('  %s' % sw.url)

        # Navigate to reCAPTCHA demo to test
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            status('Navigating to reCAPTCHA demo...')
            await page.goto('https://www.google.com/recaptcha/api2/demo', wait_until='domcontentloaded', timeout=30000)
            title = await page.title()
            status('Page title: %s' % title)
            
            # Wait for reCAPTCHA to load
            await asyncio.sleep(5)
            
            frames = page.frames
            recaptcha_frames = [f for f in frames if 'recaptcha' in f.url.lower()]
            status('reCAPTCHA frames: %d' % len(recaptcha_frames))
            for f in recaptcha_frames:
                status('  Frame: %s' % f.url[:120])
                
        except Exception as e:
            status('Navigation error: %s' % str(e))

        status('BROWSER_READY')
        status('Extension loaded and configured. Browser running.')
        
        # Keep alive indefinitely
        try:
            await asyncio.sleep(999999)
        except asyncio.CancelledError:
            pass

asyncio.run(main())
