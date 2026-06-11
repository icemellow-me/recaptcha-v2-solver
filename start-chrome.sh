#!/bin/bash
# start-chrome.sh — Launch Chrome with CaptchaPlugin extension (WS addon mode)
set -e

EXT_DIR="/opt/captchaplugin/extension"
PROFILE_DIR="/opt/captchaplugin/chrome-profile"
CDP_PORT=9222

# Create profile dir if needed
mkdir -p "$PROFILE_DIR"

# Kill any existing Chrome instances
pkill -f 'google-chrome.*captchaplugin' 2>/dev/null || true
sleep 1

# Launch Chrome with the extension loaded
# --headless=new supports extensions (Chrome 116+)
google-chrome-stable \
  --headless=new \
  --disable-gpu \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-software-rasterizer \
  --remote-debugging-port=$CDP_PORT \
  --remote-debugging-address=0.0.0.0 \
  --user-data-dir="$PROFILE_DIR" \
  --load-extension="$EXT_DIR" \
  --disable-extensions-except="$EXT_DIR" \
  --no-first-run \
  --no-default-browser-check \
  --disable-background-networking \
  --disable-sync \
  --disable-translate \
  --mute-audio \
  --window-size=1920,1080 \
  --enable-features=ExtensionsToolbarMenu \
  --disable-features=TranslateUI \
  &

CHROME_PID=$!
echo "Chrome started with PID $CHROME_PID"
echo "CDP endpoint: http://localhost:$CDP_PORT"

# Wait for CDP to become available
for i in $(seq 1 30); do
  if curl -s "http://localhost:$CDP_PORT/json/version" > /dev/null 2>&1; then
    echo "CDP is ready!"
    curl -s "http://localhost:$CDP_PORT/json/version" | head -5
    exit 0
  fi
  sleep 1
done

echo "ERROR: CDP did not become available within 30 seconds"
exit 1
