#!/usr/bin/env python3
"""Launch Chrome with CaptchaPlugin extension using Playwright persistent context
FIX: Also ignore --disable-component-extensions-with-background-pages"""
import asyncio
from playwright.async_api import async_playwright

EXTENSION_PATH = '/opt/captchaplugin/extension'
STATUS_FILE = '/tmp/captcha-browser-status.txt'

def status(msg):
    with open(STATUS_FILE, 'a') as f:
        f.write(msg + '\n')
        f.flush()
    print(msg, flush=True)

async def main():
    status('Starting Playwright...')
    
    async with async_playwright() as p:
        status('Launching Chrome with extension...')
        
        context = await p.chromium.launch_persistent_context(
            user_data_dir='/tmp/captcha-playwright-v4',
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
            # KEY FIX: remove BOTH --disable-extensions AND --disable-component-extensions-with-background-pages
            ignore_default_args=[
                '--disable-extensions',
                '--disable-component-extensions-with-background-pages',
            ],
            executable_path='/usr/bin/google-chrome-stable',
        )

        status('Browser launched. Waiting 15s for extension...')
        await asyncio.sleep(15)

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

        # Check pages
        pages = context.pages
        status('Pages: %d' % len(pages))
        for page in pages:
            status('  Page: %s' % page.url)

        # Navigate to reCAPTCHA demo
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            status('Navigating to reCAPTCHA demo...')
            await page.goto('https://www.google.com/recaptcha/api2/demo', wait_until='domcontentloaded', timeout=30000)
            title = await page.title()
            status('Page title: %s' % title)
            status('Page URL: %s' % page.url)
            
            # Check for reCAPTCHA frames
            frames = page.frames
            recaptcha_frames = [f for f in frames if 'recaptcha' in f.url.lower()]
            status('reCAPTCHA frames: %d' % len(recaptcha_frames))
            for f in recaptcha_frames:
                status('  Frame: %s' % f.url[:100])
                
        except Exception as e:
            status('Navigation error: %s' % str(e))

        status('BROWSER_READY')
        
        # Keep alive
        try:
            await asyncio.sleep(999999)
        except asyncio.CancelledError:
            pass

asyncio.run(main())
