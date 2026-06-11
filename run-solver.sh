#!/bin/bash
# run-solver.sh — Full CaptchaPlugin solver setup and run
set -e

CDP_PORT=9222
API_KEY="8010000000ccojr5nrbg516w5jvw1wu9"

echo "=== CaptchaPlugin Solver Setup ==="

# Step 1: Start Chrome with extension
echo "[1/3] Starting Chrome with CaptchaPlugin extension..."
bash /opt/captchaplugin/start-chrome.sh

# Step 2: Configure the extension via CDP (set API key + enable WS mode)
echo ""
echo "[2/3] Configuring extension (API key + WS mode)..."

# Use CDP to evaluate JS in the extension's service worker context
# First, find the extension's service worker target
SW_TARGET=$(curl -s "http://localhost:$CDP_PORT/json" | python3 -c "
import json, sys
targets = json.load(sys.stdin)
for t in targets:
    if t.get('type') == 'service_worker' and 'captchaplugin' in t.get('url', '').lower():
        print(t['webSocketDebuggerUrl'])
        break
else:
    # Fallback: look for any service_worker
    for t in targets:
        if t.get('type') == 'service_worker':
            print(t['webSocketDebuggerUrl'])
            break
" 2>/dev/null)

if [ -z "$SW_TARGET" ]; then
    echo "WARNING: Could not find extension service worker target."
    echo "The extension may need a moment to initialize. Trying page target instead..."
    SW_TARGET=$(curl -s "http://localhost:$CDP_PORT/json" | python3 -c "
import json, sys
targets = json.load(sys.stdin)
for t in targets:
    if t.get('type') == 'page':
        print(t['webSocketDebuggerUrl'])
        break
" 2>/dev/null)
fi

echo "Service worker target: $SW_TARGET"

# Step 3: Health check
echo ""
echo "[3/3] Running health check..."
HEALTH=$(curl -s "http://localhost:$CDP_PORT/json/version")
if [ -n "$HEALTH" ]; then
    echo "Chrome is running and CDP is accessible"
    echo "$HEALTH" | python3 -m json.tool 2>/dev/null || echo "$HEALTH"
    echo ""
    echo "=== CaptchaPlugin Solver is READY ==="
    echo "API endpoint: http://localhost:$CDP_PORT (CDP)"
    echo "API key: $API_KEY"
    echo ""
    echo "To submit a captcha task:"
    echo "  curl -X POST https://api.captchaplugin.com/in.php -d 'key=$API_KEY&method=userrecaptcha&googlekey=SITE_KEY&pageurl=PAGE_URL&json=1'"
    echo ""
    echo "To check result:"
    echo "  curl 'https://api.captchaplugin.com/res.php?action=get&id=TASK_ID&key=$API_KEY&json=1'"
else
    echo "ERROR: Chrome CDP not accessible"
    exit 1
fi
