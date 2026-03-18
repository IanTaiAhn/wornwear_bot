# Worn Wear Bot — DigitalOcean Deployment Guide

A step-by-step guide to get the bot running 24/7 on a DigitalOcean droplet.

---

## What You'll Need

- A DigitalOcean account (sign up at digitalocean.com — new accounts get $200 free credit)
- Your local bot project folder ready to go
- An SSH key pair (we'll generate one if you don't have one)
- Your `.env` file configured with keywords and notify URL

---

## Step 1 — Create Your Droplet

1. Log in to DigitalOcean and click **Create → Droplets**
2. Choose the following settings:
   - **Region:** pick whichever is closest to you
   - **OS:** Ubuntu 24.04 LTS
   - **Droplet type:** Basic (Shared CPU)
   - **Plan:** Regular → **$6/mo** (1GB RAM / 1 vCPU / 25GB SSD)
     > The 512MB/$4 option is too tight for Playwright. The $6/mo 1GB plan is the safe minimum.
3. **Authentication:** choose SSH Key (more secure than a password)
   - If you don't have an SSH key yet, run this on your local machine:
     ```bash
     ssh-keygen -t ed25519 -C "wornwear-bot"
     ```
   - Press Enter to accept the default file location (`~/.ssh/id_ed25519`)
   - Copy your public key to paste into DigitalOcean:
     ```bash
     cat ~/.ssh/id_ed25519.pub
     ```
   - Click **New SSH Key** in DigitalOcean, paste it in, and save
4. Give the droplet a hostname like `wornwear-bot`
5. Click **Create Droplet** and wait ~60 seconds for it to spin up
6. Copy the droplet's IP address from the dashboard — you'll use it throughout this guide

---

## Step 2 — First Login & System Setup

SSH into your new droplet:

```bash
ssh root@YOUR_DROPLET_IP
```

Update the system and install required packages:

```bash
apt update && apt upgrade -y

# Chromium system dependencies needed by Playwright
apt install -y \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2
```

Create a non-root user to run the bot (better security practice than running as root):

```bash
adduser ubuntu
usermod -aG sudo ubuntu

# Copy your SSH key to the new user so you can log in as them
rsync --archive --chown=ubuntu:ubuntu ~/.ssh /home/ubuntu
```

---

## Step 3 — Install uv

Log in as the `ubuntu` user:

```bash
su - ubuntu
```

Install uv:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

# Load uv into your current shell session
source $HOME/.local/bin/env
```

Verify it installed correctly:

```bash
uv --version
```

---

## Step 4 — Upload Your Bot Files

On your **local machine** (open a new terminal tab, don't close the server one), upload the project folder:

```bash
scp -r /path/to/your/wornwear-bot ubuntu@YOUR_DROPLET_IP:/home/ubuntu/wornwear-bot
```

> Replace `/path/to/your/wornwear-bot` with the actual path to your local project folder.

Back on the **server**, verify the files arrived:

```bash
ls /home/ubuntu/wornwear-bot
```

You should see `bot.py`, `pyproject.toml`, `uv.lock`, `.env.example`, and `wornwear-bot.service`.

---

## Step 5 — Configure the Bot

```bash
cd /home/ubuntu/wornwear-bot

# Create your .env from the template
cp .env.example .env
nano .env
```

Set your values — at minimum:

```
KEYWORDS=synchilla,fleece,medium
NOTIFY_URL=https://ntfy.sh/your-unique-topic-name
AUTO_ADD_CART=false
```

Save and exit nano with `Ctrl+O`, then `Enter`, then `Ctrl+X`.

---

## Step 6 — Install Dependencies & Playwright

```bash
cd /home/ubuntu/wornwear-bot

# Install Python dependencies
uv sync

# Install Chromium browser for Playwright
uv run playwright install chromium
```

---

## Step 7 — Do a Test Run

Before setting up the service, confirm the bot works:

```bash
cd /home/ubuntu/wornwear-bot
uv run python bot.py
```

You should see log output like:

```
2024-01-15 10:23:01  INFO     Bot started. Keywords: ['synchilla', 'fleece', 'medium']
2024-01-15 10:23:01  INFO     Poll interval: 30–65s  |  Auto-cart: False
2024-01-15 10:23:01  INFO     Checking https://wornwear.patagonia.com/collections/mens-fleece …
2024-01-15 10:23:06  INFO       Found 24 products on page
2024-01-15 10:23:06  INFO     Sleeping 47s …
```

If it's working, stop it with `Ctrl+C` and move on to the next step.

---

## Step 8 — Install as a System Service

Copy the service file and edit it to match your setup:

```bash
sudo cp /home/ubuntu/wornwear-bot/wornwear-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/wornwear-bot.service
```

Update the `ExecStart` line to use `uv`:

```ini
[Unit]
Description=Worn Wear Monitor Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/wornwear-bot
ExecStart=/home/ubuntu/.local/bin/uv run python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

> The key change is `ExecStart` — it now uses the full path to `uv` so systemd can find it.

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X`), then enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable wornwear-bot    # start automatically on boot
sudo systemctl start wornwear-bot     # start right now
```

---

## Step 9 — Verify It's Running

Check the service status:

```bash
sudo systemctl status wornwear-bot
```

You should see `active (running)` in green. To watch the live log stream:

```bash
journalctl -u wornwear-bot -f
```

Press `Ctrl+C` to stop watching logs (the bot keeps running).

---

## Useful Commands Reference

| Task                    | Command                                            |
| ----------------------- | -------------------------------------------------- |
| Check if bot is running | `sudo systemctl status wornwear-bot`               |
| Watch live logs         | `journalctl -u wornwear-bot -f`                    |
| Stop the bot            | `sudo systemctl stop wornwear-bot`                 |
| Restart the bot         | `sudo systemctl restart wornwear-bot`              |
| View recent logs        | `journalctl -u wornwear-bot -n 50`                 |
| Edit keywords           | `nano /home/ubuntu/wornwear-bot/.env` then restart |

---

## Updating the Bot

When you make changes locally and want to push them to the server:

```bash
# From your local machine — upload changed files
scp -i ~/.ssh/wornwear-bot -r /c/Users/ianta/wornwear_bot root@64.23.131.88:/root/wornwear-bot

# Then on the server, restart the service to pick up the changes
sudo systemctl restart wornwear-bot
```

---

## Changing Keywords

You don't need to touch any code — just edit `.env` on the server and restart:

```bash
nano /home/ubuntu/wornwear-bot/.env
# make your changes, save and exit
sudo systemctl restart wornwear-bot
```

---

## Troubleshooting

**Bot fails to start / Playwright errors**
Run `journalctl -u wornwear-bot -n 100` to see the full error. If it's a missing library, re-run the `apt install` command from Step 2.

**"uv: command not found" in the service**
The `ExecStart` path must be the full absolute path: `/home/ubuntu/.local/bin/uv`. Double-check Step 8.

**No products found (0 products on page)**
The Worn Wear site is a React app and occasionally changes its markup. Try running `uv run python test_bot.py` to debug what the scraper sees.

**Bot keeps restarting / crashing**
Check `journalctl -u wornwear-bot -n 100` for the error. Common cause is not enough RAM — make sure you're on the $6/mo 1GB plan, not the 512MB one.

**Not getting notifications**
Confirm your `NOTIFY_URL` in `.env` is correct and that you're subscribed to the same topic name in the ntfy app.
