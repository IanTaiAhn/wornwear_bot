# Securing noVNC with Cloudflare Tunnel

Add encrypted HTTPS access to your noVNC remote browser — free, no nginx config,
no certificates to manage. Everything runs on your DigitalOcean droplet.
Nothing changes on your phone.

---

## Why Bother?

Without a tunnel, your phone talks to the droplet over plain HTTP. That means
your VNC password and everything you type during checkout (including card details)
travels unencrypted. Even on 5G/LTE the traffic crosses the open internet.

Cloudflare Tunnel fixes this by creating an outbound encrypted connection from
your droplet to Cloudflare's network. Your phone connects to Cloudflare over
HTTPS, and Cloudflare forwards it through the tunnel to noVNC on localhost.
The droplet never exposes port 6080 to the internet at all.

**Cost: Free.** Cloudflare Tunnel, TLS certificates, and DDoS protection are
all included on Cloudflare's free tier.

---

## Two Options

| | Quick Tunnel (no domain) | Named Tunnel (with domain) |
|---|---|---|
| **Cost** | Completely free | ~$10/year for a domain |
| **URL** | Random, changes every restart | Stable (e.g. `vnc.yourdomain.com`) |
| **Setup time** | 5 minutes | 15 minutes |
| **Good for** | Testing, proving it works | Actual daily use |
| **Downside** | URL changes = ntfy link breaks on restart | Need to buy a domain |

**Recommendation:** Start with Quick Tunnel to verify everything works. If you
like it, buy a cheap domain and switch to a Named Tunnel so your ntfy links
stay permanent.

---

## Quick Tunnel (No Domain, No Account)

This is the fastest way to test. One command, no Cloudflare account needed.

### Step 1 — Install cloudflared on the droplet

```bash
# Download and install
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
  -o /tmp/cloudflared.deb
dpkg -i /tmp/cloudflared.deb

# Verify
cloudflared --version
```

### Step 2 — Run the tunnel

Make sure noVNC is already running on port 6080 (your existing setup), then:

```bash
cloudflared tunnel --url http://localhost:6080
```

You'll see output like:

```
INF Requesting new quick Tunnel on trycloudflare.com...
INF +--------------------------------------------------------------------------------------------+
INF | Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
INF | https://random-words-here.trycloudflare.com                                               |
INF +--------------------------------------------------------------------------------------------+
```

### Step 3 — Test it

Open that `https://random-words-here.trycloudflare.com/vnc.html` URL on your
phone. You should see the noVNC connect screen over HTTPS — padlock icon and all.

### Step 4 — Close port 6080

Since traffic now goes through Cloudflare's tunnel (outbound from your droplet),
you no longer need port 6080 open in your DigitalOcean firewall. Remove the
inbound rule for port 6080. This means no one can reach noVNC directly — they
must go through Cloudflare.

### The Catch

Every time cloudflared restarts, you get a new random URL. That means the noVNC
link in your ntfy notifications will be wrong after a reboot. This is fine for
testing but not for daily use. For a stable URL, set up a Named Tunnel below.

---

## Named Tunnel (Stable URL, Recommended for Daily Use)

This gives you a permanent URL like `https://vnc.yourdomain.com` that survives
reboots, runs as a systemd service, and auto-starts with your droplet.

### Prerequisites

- A **Cloudflare account** (free at [dash.cloudflare.com](https://dash.cloudflare.com))
- A **domain name** pointed at Cloudflare's nameservers

> **Getting a cheap domain:** Search for `.xyz`, `.site`, or `.online` domains
> on any registrar (Cloudflare Registrar, Namecheap, Porkbun). Many are under
> $5/year for the first year. Once purchased, transfer DNS to Cloudflare or
> register directly through Cloudflare Registrar (no markup — they charge
> wholesale price).

### Step 1 — Install cloudflared

Same as above if you haven't already:

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
  -o /tmp/cloudflared.deb
dpkg -i /tmp/cloudflared.deb
```

### Step 2 — Authenticate with Cloudflare

```bash
cloudflared tunnel login
```

This prints a URL. Copy it, open it in a browser (on your laptop — not the
droplet), log into Cloudflare, and select the domain you want to use. This
saves a certificate to `/root/.cloudflared/cert.pem` on the droplet.

### Step 3 — Create a named tunnel

```bash
cloudflared tunnel create wornwear-vnc
```

Output:

```
Tunnel credentials written to: /root/.cloudflared/XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX.json
Created tunnel wornwear-vnc with id XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
```

Save that tunnel ID — you need it for the config file.

### Step 4 — Add a DNS route

```bash
cloudflared tunnel route dns wornwear-vnc vnc.yourdomain.com
```

Replace `vnc.yourdomain.com` with your actual subdomain. This creates a CNAME
record in Cloudflare DNS automatically.

### Step 5 — Create the config file

```bash
mkdir -p /etc/cloudflared
nano /etc/cloudflared/config.yml
```

Paste (replace the tunnel ID and hostname with yours):

```yaml
tunnel: XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
credentials-file: /root/.cloudflared/XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX.json

ingress:
  - hostname: vnc.yourdomain.com
    service: http://localhost:6080
  - service: http_status:404
```

### Step 6 — Test it manually first

```bash
cloudflared tunnel run wornwear-vnc
```

Open `https://vnc.yourdomain.com/vnc.html` on your phone. Confirm you see the
noVNC connect screen with HTTPS. Press Ctrl+C to stop.

### Step 7 — Install as a systemd service

```bash
cloudflared --config /etc/cloudflared/config.yml service install
systemctl enable cloudflared
systemctl start cloudflared

# Verify
systemctl status cloudflared
```

The tunnel now starts automatically on boot, reconnects if dropped, and runs
alongside your existing xvfb/x11vnc/novnc/wornwear-bot services.

### Step 8 — Lock down the firewall

Remove the port 6080 inbound rule from your DigitalOcean firewall. All traffic
now flows through the encrypted tunnel. Port 6080 only needs to be accessible
on localhost (which it is by default since noVNC listens on 0.0.0.0, but
Cloudflare connects from inside the droplet via the tunnel daemon).

For extra safety, make noVNC listen only on localhost by editing the noVNC
systemd service:

```bash
nano /etc/systemd/system/novnc.service
```

Change the ExecStart line:

```ini
ExecStart=/opt/noVNC/utils/novnc_proxy \
    --vnc localhost:5900 \
    --listen 127.0.0.1:6080
```

Then:

```bash
systemctl daemon-reload
systemctl restart novnc
```

Now noVNC is only reachable through the Cloudflare tunnel — not directly from
the internet even if someone finds your droplet's IP.

---

## Update bot.py for the New URL

Replace the noVNC URL in your bot code. Instead of:

```python
novnc_url = f"http://{DROPLET_IP}:6080/vnc.html?autoconnect=true"
```

Use:

```python
# For Named Tunnel (recommended)
NOVNC_HOST = os.getenv("NOVNC_HOST", "")  # e.g. vnc.yourdomain.com
novnc_url = f"https://{NOVNC_HOST}/vnc.html?autoconnect=true" if NOVNC_HOST else ""
```

Add to `.env`:

```bash
NOVNC_HOST=vnc.yourdomain.com
```

Now your ntfy notifications will contain `https://vnc.yourdomain.com/vnc.html?autoconnect=true`
— encrypted, stable, and tappable from your phone.

---

## What This Gets You

| Before (plain HTTP) | After (Cloudflare Tunnel) |
|---|---|
| `http://IP:6080` — unencrypted | `https://vnc.yourdomain.com` — TLS encrypted |
| Port 6080 open to internet | Port 6080 closed, localhost only |
| IP-restricted firewall as only defense | Tunnel + VNC password + no exposed port |
| VNC password sent in cleartext | VNC password encrypted in transit |
| Card details visible to packet sniffers | Card details encrypted end-to-end |

---

## Useful Commands

| Task | Command |
|---|---|
| Check tunnel status | `systemctl status cloudflared` |
| View tunnel logs | `journalctl -u cloudflared -f` |
| List your tunnels | `cloudflared tunnel list` |
| Restart tunnel | `systemctl restart cloudflared` |
| Delete a tunnel | `cloudflared tunnel delete wornwear-vnc` |
| Test DNS resolution | `dig vnc.yourdomain.com` |

---

## Troubleshooting

**"Bad gateway" or 502 error in browser**
noVNC isn't running on port 6080. Check it: `systemctl status novnc`. The
tunnel is working but has nothing to forward to.

**Tunnel connects but page never loads**
Check that noVNC is listening on the right address. If you changed it to
`127.0.0.1:6080`, that's correct. If it's not listening at all:
`ss -tlnp | grep 6080`.

**"cloudflared tunnel login" doesn't open a browser**
It won't — you're on a headless server. It prints a URL. Copy that URL and
open it in a browser on your laptop or phone.

**Tunnel keeps disconnecting**
Check memory: `free -m`. cloudflared uses about 20-30MB, which shouldn't be
a problem, but combined with Chromium + noVNC + the bot on a 1GB droplet
it can get tight. Verify your swap file is active: `swapon --show`.

**Quick Tunnel URL changed after reboot**
This is expected — quick tunnels get a new random URL every time. Switch to
a Named Tunnel for a permanent URL.

**Domain not resolving**
Make sure your domain's nameservers are pointed at Cloudflare (check in the
Cloudflare dashboard under your domain's DNS settings). The CNAME record
should have been created automatically by the `tunnel route dns` command.
Verify with: `cloudflared tunnel route list`.
