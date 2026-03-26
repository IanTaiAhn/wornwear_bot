# Worn Wear Bot — DigitalOcean Deployment Guide

A step-by-step guide to get the bot running 24/7 on a DigitalOcean droplet.

---

## What You'll Need

- A DigitalOcean account (sign up at digitalocean.com — new accounts get $200 free credit)
- Your local bot project folder ready to go
- An SSH key pair (we'll generate one if you don't have one)
- Your `.env` file configured with keywords and notify URL

## User Choice: root vs ubuntu

This guide covers setup for **both** root and ubuntu users:

- **root** (simpler, faster setup):
  - Default user on DigitalOcean droplets
  - Skip Step 2's user creation steps
  - Use `/root/wornwear-bot` paths throughout
  - Less secure but fine for personal projects

- **ubuntu** (more secure, recommended):
  - Follow Step 2 to create a non-root user
  - Use `/home/ubuntu/wornwear-bot` paths
  - Better security practice for production
  - Slightly more setup steps

Choose one approach and stick with it throughout the guide.

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

**(Optional but recommended)** Create a non-root user to run the bot:

> If you're running as root, **skip this section** and proceed to Step 3.

```bash
adduser ubuntu
usermod -aG sudo ubuntu

# Copy your SSH key to the new user so you can log in as them
rsync --archive --chown=ubuntu:ubuntu ~/.ssh /home/ubuntu
```

---

## Step 3 — Install uv

If you created an `ubuntu` user, log in as them:

```bash
su - ubuntu
```

If you're running as `root`, stay logged in as root.

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

**If running as ubuntu:**
```bash
scp -r /path/to/your/wornwear-bot ubuntu@YOUR_DROPLET_IP:/home/ubuntu/wornwear-bot
```

**If running as root:**
```bash
scp -r /path/to/your/wornwear-bot root@YOUR_DROPLET_IP:/root/wornwear-bot
```

> Replace `/path/to/your/wornwear-bot` with the actual path to your local project folder.

Back on the **server**, verify the files arrived:

**If ubuntu:** `ls /home/ubuntu/wornwear-bot`
**If root:** `ls /root/wornwear-bot`

You should see `bot.py`, `pyproject.toml`, `uv.lock`, `.env.example`, and `wornwear-bot.service`.

---

## Step 5 — Configure the Bot

Navigate to your bot directory:
- **If ubuntu:** `cd /home/ubuntu/wornwear-bot`
- **If root:** `cd /root/wornwear-bot`

```bash
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

From your bot directory:

```bash
# Install Python dependencies
uv sync

# Install Chromium browser for Playwright
uv run playwright install chromium
```

---

## Step 7 — Do a Test Run

Before setting up the service, confirm the bot works:

```bash
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

Copy the service file template to systemd:

**If running as root:**
```bash
sudo cp /root/wornwear-bot/wornwear-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/wornwear-bot.service
```

**If running as ubuntu:**
```bash
sudo cp /home/ubuntu/wornwear-bot/wornwear-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/wornwear-bot.service
```

**Replace the entire file content** with the correct configuration for your user:

### If running as `root`:

```ini
[Unit]
Description=Worn Wear Monitor Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/wornwear-bot
ExecStart=/root/.local/bin/uv run python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### If running as `ubuntu`:

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

> **Key points:**
> - `User=` must match who you're logged in as
> - `WorkingDirectory=` must match where your bot files are
> - `ExecStart=` uses the full path to `uv` (not a venv) so systemd can find it
> - The template service file in the repo is outdated — always use the config above

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

| Task                    | Command                                                      |
| ----------------------- | ------------------------------------------------------------ |
| Check if bot is running | `systemctl status wornwear-bot` (add `sudo` if using ubuntu)|
| Watch live logs         | `journalctl -u wornwear-bot -f`                              |
| Stop the bot            | `systemctl stop wornwear-bot` (add `sudo` if using ubuntu)  |
| Restart the bot         | `systemctl restart wornwear-bot` (add `sudo` if using ubuntu)|
| View recent logs        | `journalctl -u wornwear-bot -n 50`                           |
| Edit keywords (ubuntu)  | `nano /home/ubuntu/wornwear-bot/.env` then restart          |
| Edit keywords (root)    | `nano /root/wornwear-bot/.env` then restart                 |

---

## Updating the Bot

When you make changes locally and want to push them to the server:

```bash
# From your local machine — upload changed files
# If running as root:
scp -r /path/to/local/wornwear_bot root@YOUR_DROPLET_IP:/root/wornwear-bot

# If running as ubuntu:
scp -r /path/to/local/wornwear_bot ubuntu@YOUR_DROPLET_IP:/home/ubuntu/wornwear-bot

# OR if using git (recommended):
# On your server, pull latest changes:
ssh root@YOUR_DROPLET_IP "cd /root/wornwear-bot && git pull"

# Then on the server, restart the service to pick up the changes
sudo systemctl restart wornwear-bot
```

---

## Changing Keywords

You don't need to touch any code — just edit `.env` on the server and restart:

**If ubuntu:**
```bash
nano /home/ubuntu/wornwear-bot/.env
# make your changes, save and exit
sudo systemctl restart wornwear-bot
```

**If root:**
```bash
nano /root/wornwear-bot/.env
# make your changes, save and exit
systemctl restart wornwear-bot
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
