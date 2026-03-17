# Worn Wear Monitor Bot

Monitors wornwear.patagonia.com for items matching your keywords and
optionally adds them to cart automatically. Sends push notifications
to your phone the moment a match is found.

---

## Files

```
wornwear-bot/
├── bot.py                  # Main script
├── pyproject.toml          # Project config and dependencies
├── uv.lock                 # Locked dependency versions
├── .env.example            # Config template — copy to .env
├── wornwear-bot.service    # systemd unit (for VPS hosting)
└── seen_items.json         # Auto-created — tracks seen products
```

---

## Local Setup (test on your machine first)

```bash
# 1. Clone / copy files into a folder
cd wornwear-bot

# 2. Install dependencies and set up the environment
uv sync

# 3. Install Chromium for Playwright
uv run playwright install chromium

# 4. Configure
cp .env.example .env
nano .env                      # Set your KEYWORDS and NOTIFY_URL

# 5. Run
uv run python bot.py
```

---

## VPS Deployment (DigitalOcean / Hetzner / etc.)

### 1. Spin up a server
- DigitalOcean: $4/mo "Basic Droplet", Ubuntu 22.04
- Hetzner: €3.29/mo CX11, even cheaper

### 2. SSH in and set up
```bash
ssh root@your-server-ip

# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

# Install Chromium dependencies for Playwright
apt update && apt install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2

# Copy your bot files up (from your local machine)
# scp -r ./wornwear-bot root@your-server-ip:/home/ubuntu/

# Install dependencies on the server
cd /home/ubuntu/wornwear-bot
uv sync
uv run playwright install chromium
```

### 3. Configure
```bash
cp .env.example .env
nano .env     # Set KEYWORDS, NOTIFY_URL, AUTO_ADD_CART
```

### 4. Install as a system service (runs forever, restarts on crash)
```bash
cp wornwear-bot.service /etc/systemd/system/
# Edit the file if your username isn't "ubuntu"
# Also update ExecStart to use uv:
#   ExecStart=/home/ubuntu/.local/bin/uv run python bot.py
nano /etc/systemd/system/wornwear-bot.service

systemctl daemon-reload
systemctl enable wornwear-bot
systemctl start wornwear-bot
```

### 5. Check it's running
```bash
systemctl status wornwear-bot
journalctl -u wornwear-bot -f    # live log stream
```

---

## Push Notifications (ntfy.sh)

The easiest way to get phone alerts — free, no account needed.

1. Install the **ntfy** app on your phone (iOS or Android)
2. Pick a unique topic name, e.g. `wornwear-alerts-xyz789`
3. Subscribe to it in the app
4. Set in `.env`:
   ```
   NOTIFY_URL=https://ntfy.sh/wornwear-alerts-xyz789
   ```

You'll get a push notification the instant a match is found.

---

## Tuning Keywords

All keywords must match for an alert to fire:

```
KEYWORDS=synchilla,medium          # matches "Men's Synchilla Fleece - Medium"
KEYWORDS=nano puff,small,womens    # more specific
KEYWORDS=r1,full-zip               # targets R1 full-zips only
```

---

## Notes

- The bot only notifies (AUTO_ADD_CART=false by default). Set to true only
  if you trust it — the add-to-cart selectors may need updating if Patagonia
  changes their site markup.
- seen_items.json prevents re-alerting on the same items across restarts.
- Logs are written to bot.log and also to systemd journal on a VPS.