# noVNC Remote Browser — Implementation Guide

Get the Worn Wear bot's Chromium browser accessible from your phone so you can
complete checkout the moment an item is bagged.

---

## How It Works

```
Bot finds item → adds to cart → ntfy fires with link
    ↓
You tap link on phone → noVNC in browser → live Chromium session
    ↓
Complete checkout manually in the bot's browser
```

The stack:

- **Xvfb** — virtual display (fake screen the server renders to)
- **x11vnc** — VNC server that streams that virtual display
- **noVNC + websockify** — translates VNC into a websocket so your phone's browser can connect with no app needed

---

## Step 1 — Add a Swap File

Before adding anything, set up a 1GB swap file as a memory safety net.
Do this first — it takes 2 minutes and could save you from a crash mid-checkout.

```bash
# Create a 1GB swap file
fallocate -l 1G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile

# Make it persist across reboots
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# Verify
free -m   # Swap row should now show 1024 total
```

---

## Step 2 — Install Dependencies

```bash
apt update

# Xvfb (virtual display) + x11vnc (VNC server) + fonts
apt install -y xvfb x11vnc xterm fonts-liberation

# noVNC and websockify
git clone https://github.com/novnc/noVNC.git /opt/noVNC
git clone https://github.com/novnc/websockify.git /opt/noVNC/utils/websockify

# Make the websockify launcher executable
chmod +x /opt/noVNC/utils/websockify/run
```

Verify noVNC installed correctly:

```bash
ls /opt/noVNC/vnc.html    # should exist
```

---

## Step 3 — Set a VNC Password

You don't want an open VNC on a public IP. Set a password now.

```bash
mkdir -p /root/.vnc
x11vnc -storepasswd YOUR_PASSWORD_HERE /root/.vnc/passwd
```

Replace `YOUR_PASSWORD_HERE` with something strong. You'll enter this on your
phone when connecting.

---

## Step 4 — Create systemd Services

You need three services running in order:

1. `xvfb` — starts the virtual display
2. `x11vnc` — streams it over VNC
3. `novnc` — exposes it as a web page
4. `wornwear-bot` — runs the bot inside that display (update existing service)

### 4a. Xvfb service

```bash
nano /etc/systemd/system/xvfb.service
```

Paste:

```ini
[Unit]
Description=Virtual Frame Buffer (Xvfb)
After=network.target

[Service]
ExecStart=/usr/bin/Xvfb :99 -screen 0 1280x800x24
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 4b. x11vnc service

```bash
nano /etc/systemd/system/x11vnc.service
```

Paste:

```ini
[Unit]
Description=x11vnc VNC Server
After=xvfb.service
Requires=xvfb.service

[Service]
ExecStart=/usr/bin/x11vnc \
    -display :99 \
    -rfbauth /root/.vnc/passwd \
    -rfbport 5900 \
    -forever \
    -noxdamage \
    -shared
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 4c. noVNC service

```bash
nano /etc/systemd/system/novnc.service
```

Paste:

```ini
[Unit]
Description=noVNC Web Client
After=x11vnc.service
Requires=x11vnc.service

[Service]
ExecStart=/opt/noVNC/utils/novnc_proxy \
    --vnc localhost:5900 \
    --listen 6080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 4d. Update the bot service

Edit the existing bot service to run Chromium inside the virtual display:

```bash
nano /etc/systemd/system/wornwear-bot.service
```

Add the `DISPLAY` environment variable:

```ini
[Unit]
Description=Worn Wear Monitor Bot
After=novnc.service
Requires=novnc.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/wornwear-bot
Environment=DISPLAY=:99
ExecStart=/root/.local/bin/uv run python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

> Adjust `User` and `WorkingDirectory` to match your actual setup if you're
> running as `ubuntu` instead of `root`.

---

## Step 5 — Update bot.py

Two changes needed: launch Chromium with `headless=False` and send the noVNC
URL in the ntfy notification.

### Change 1 — headless=False

Find this block in `bot.py`:

```python
browser = await pw.chromium.launch(
    headless=True,
    args=["--no-sandbox", "--disable-dev-shm-usage"],
)
```

Change to:

```python
browser = await pw.chromium.launch(
    headless=False,
    args=[
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--display=:99",
    ],
)
```

### Change 2 — Include the noVNC URL in the match notification

Add your droplet IP to your `.env` file:

```
DROPLET_IP=YOUR_DROPLET_IP_HERE
```

Then in `bot.py`, near the top where other env vars are loaded:

```python
DROPLET_IP = os.getenv("DROPLET_IP", "")
```

Find the `await notify(...)` call that fires on a match and update the body:

```python
novnc_url = f"http://{DROPLET_IP}:6080/vnc.html?autoconnect=true" if DROPLET_IP else ""

await notify(
    title="Worn Wear Match — Item Bagged!",
    body=(
        f"{title} — {product.get('price', '')}\n"
        f"Matched: {', '.join(match_reason)}\n"
        f"{product.get('url', '')}\n"
        f"\n🖥️ Checkout here (30 min window):\n{novnc_url}"
    ),
)
```

### Change 3 — 25-minute warning notification

Add a background task that fires a second notification after 25 minutes.
Add this helper function to `bot.py`:

```python
async def cart_expiry_warning(title: str, url: str, delay_seconds: int = 1500):
    """Fire a warning notification before the cart expires (default 25 min)."""
    await asyncio.sleep(delay_seconds)
    novnc_url = f"http://{DROPLET_IP}:6080/vnc.html?autoconnect=true" if DROPLET_IP else ""
    await notify(
        title="⚠️ Cart expiring in 5 minutes!",
        body=(
            f"{title} — cart expires soon!\n"
            f"🖥️ Checkout now: {novnc_url}"
        ),
    )
```

Then immediately after the match notification fires, schedule the warning:

```python
# Schedule the 25-minute expiry warning (non-blocking)
asyncio.create_task(cart_expiry_warning(title, product.get("url", "")))
```

---

## Step 6 — Enable and Start Everything

```bash
systemctl daemon-reload

# Enable all services to start on boot
systemctl enable xvfb x11vnc novnc wornwear-bot

# Start them in order
systemctl start xvfb
systemctl start x11vnc
systemctl start novnc
systemctl start wornwear-bot

# Verify all four are running
systemctl status xvfb x11vnc novnc wornwear-bot
```

---

## Step 7 — Open Port 6080

DigitalOcean's firewall (if you have one enabled) needs to allow port 6080.
In the DigitalOcean dashboard:

1. Go to **Networking → Firewalls**
2. Select your droplet's firewall (or create one)
3. Add an **Inbound Rule**: TCP, port 6080, source: Your IP only

> **Important:** Restrict to your IP, not 0.0.0.0/0. noVNC has a password but
> limiting the source is an extra layer of protection. Your IP can be found at
> whatismyip.com. If your home IP changes frequently, consider a VPN with a
> static IP instead.

---

## Step 8 — Test It

### Test the display stack first

```bash
# Check Xvfb is running on display :99
DISPLAY=:99 xterm &   # should not error
```

### Test noVNC in your browser

On your laptop, open:

```
http://YOUR_DROPLET_IP:6080/vnc.html
```

You should see the noVNC connect screen. Enter your VNC password and you should
see a desktop with Chromium open (once the bot has started a polling cycle).

### Test on your phone

Tap the same URL in Safari or Chrome on your phone. noVNC works in mobile
browsers — you can tap to click and use the on-screen keyboard for form fields.

---

## Checkout Flow (When a Match Fires)

1. You get an ntfy notification: **"Worn Wear Match — Item Bagged!"** with the noVNC link
2. Tap the link → noVNC opens in your phone browser
3. Enter your VNC password
4. You see the live Chromium browser — navigate to the cart if needed
5. Fill in guest checkout: name, email, shipping, payment
6. Place the order
7. You get a second ntfy at 25 minutes as a backup reminder

---

## Useful Commands

| Task | Command |
|---|---|
| Watch live logs | `journalctl -u wornwear-bot -f` |
| Check memory | `free -m` |
| Restart everything | `systemctl restart xvfb x11vnc novnc wornwear-bot` |
| Stop bot only | `systemctl stop wornwear-bot` |
| Check all service status | `systemctl status xvfb x11vnc novnc wornwear-bot` |
| Check VNC is listening | `ss -tlnp \| grep 5900` |
| Check noVNC is listening | `ss -tlnp \| grep 6080` |

---

## Troubleshooting

**noVNC page loads but screen is black**
The bot hasn't launched Chromium yet (happens between poll cycles). Wait for
the next cycle or check `journalctl -u wornwear-bot -f` to see what it's doing.

**"Connection refused" on port 6080**
Check noVNC is running: `systemctl status novnc`. Also confirm port 6080 is
open in your DigitalOcean firewall.

**VNC password not working**
Re-run `x11vnc -storepasswd YOUR_PASSWORD /root/.vnc/passwd` and restart
the x11vnc service.

**Bot crashes after switching to headless=False**
Check memory with `free -m`. If available is below 150MB, the swap file should
help — verify it's active with `swapon --show`. If still crashing, consider
upgrading to the $12/mo 2GB droplet.

**noVNC is too slow on mobile**
Add `&quality=3&compression=9` to the URL to reduce bandwidth:
`http://YOUR_IP:6080/vnc.html?autoconnect=true&quality=3&compression=9`
