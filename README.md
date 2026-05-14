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

> **TODO (droplet):** Make sure your `.env` on the droplet includes these variables:
> ```
> KEYWORDS=
> STYLE_NUMBERS=*vintage*
> ACTIVE_START=7
> ACTIVE_END=23
> ```
> `KEYWORDS` empty = style-number-only matching (no false positives from titles).
> `*_vintage` bags any item whose URL contains `_NNNNN_vintage_`.

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

## Deployment Strategy: Targeting Vintage Grails

### Problem
The `just-added` collection has 15,000+ items. The bot's pagination stops after ~3 consecutive clicks with no new unique products, so it only harvests a few hundred to ~2,000 items max. Vintage grails buried deep in the catalog get missed.

### Solution: Use Targeted Search URLs

Use specific search queries instead of broad collections to pre-filter results:

```python
# In bot.py, update TARGET_URLS (lines 100-102):
TARGET_URLS = [
    "https://wornwear.patagonia.com/search?q=vintage+jacket+men",
    "https://wornwear.patagonia.com/search?q=vintage+synchilla",
    "https://wornwear.patagonia.com/search?q=vintage+snap-t",
    "https://wornwear.patagonia.com/search?q=vintage+retro-x",
]
```

This targets actual grail items (Synchilla, Snap-T, Retro-X fleeces) instead of scanning 15,000 items.

### Initial Setup Process

**Step 1: Prime the seen_items.json (avoid bagging hundreds of existing items)**

```bash
# On your droplet, set AUTO_ADD_CART to false
nano /root/wornwear-bot/.env
```

Set:
```bash
AUTO_ADD_CART=false
STYLE_NUMBERS=*_vintage
KEYWORDS=
```

Restart and let it run 1-2 poll cycles:
```bash
systemctl restart wornwear-bot
journalctl -u wornwear-bot -f
```

This populates `seen_items.json` with all existing vintage items without bagging them.

**Step 2: Enable auto-bagging for NEW items only**

Once `seen_items.json` is primed:
```bash
nano /root/wornwear-bot/.env
```

Set:
```bash
AUTO_ADD_CART=true
```

Restart:
```bash
systemctl restart wornwear-bot
```

Now the bot will **only bag newly added vintage grails**, not the hundreds of existing ones.

### Why This Works

- Search URLs return 100-500 items (not 15,000), so pagination completes fully
- `*_vintage` in `STYLE_NUMBERS` matches vintage variants in URL (not just color names)
- `seen_items.json` prevents re-bagging on every poll cycle
- Bot only bags items added **after** the initial priming run

---

## Notes

- The bot only notifies (AUTO_ADD_CART=false by default). Set to true only
  if you trust it — the add-to-cart selectors may need updating if Patagonia
  changes their site markup.
- seen_items.json prevents re-alerting on the same items across restarts.
- Logs are written to bot.log and also to systemd journal on a VPS.

## Useful Commands

| Task                     | Command                                            |
| ------------------------ | -------------------------------------------------- |
| Watch live logs          | `journalctl -u wornwear-bot -f`                    |
| Check bot status         | `systemctl status wornwear-bot`                    |
| Check memory             | `free -m`                                          |
| Restart everything       | `systemctl restart xvfb x11vnc novnc wornwear-bot` |
| Restart wornwear-bot     | `systemctl restart wornwear-bot`                   |
| Stop bot only            | `systemctl stop wornwear-bot`                      |
| Check all service status | `systemctl status xvfb x11vnc novnc wornwear-bot`  |
| Check VNC is listening   | `ss -tlnp \| grep 5900`                            |
| Check noVNC is listening | `ss -tlnp \| grep 6080`                            |

---
