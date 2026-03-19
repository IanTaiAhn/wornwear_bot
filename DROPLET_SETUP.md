# Droplet Deployment Guide (with noVNC)

Quick reference for deploying the updated bot to your DigitalOcean droplet.

---

## Deployment Checklist

### 1. Update `.env` on droplet

SSH into your droplet and update `/root/wornwear-bot/.env`:

```bash
ssh root@YOUR_DROPLET_IP
cd /root/wornwear-bot
nano .env
```

Add/update these lines:

```bash
# Enable VNC mode
USE_VNC=true
DROPLET_IP=YOUR_DROPLET_IP_HERE

# Make sure auto-cart is enabled
AUTO_ADD_CART=true
```

Save and exit (`Ctrl+X`, `Y`, `Enter`).

### 2. Pull the updated code

If you're using git:

```bash
git pull origin main
```

Or manually copy the updated `bot.py`:

```bash
# From your local machine
scp bot.py root@YOUR_DROPLET_IP:/root/wornwear-bot/
```

### 3. Follow the noVNC setup

Follow all steps in `novnc-implementation.md`:

- [ ] Add swap file (Step 1)
- [ ] Install Xvfb, x11vnc, noVNC (Step 2)
- [ ] Set VNC password (Step 3)
- [ ] Create systemd services (Step 4)
- [ ] Enable and start services (Step 6)
- [ ] Open port 6080 in firewall (Step 7)
- [ ] Test noVNC access (Step 8)

### 4. Verify everything is running

```bash
# Check all services are active
systemctl status xvfb x11vnc novnc wornwear-bot

# Watch live logs
journalctl -u wornwear-bot -f
```

You should see:

```
Bot started.
  Keywords:     [your keywords]
  Style #s:     [your style numbers]
  Poll interval: 30–65s  |  Auto-cart: True
  VNC mode:     enabled
  Seen items:   X from previous runs
```

### 5. Test noVNC access

On your phone or laptop, open:

```
http://YOUR_DROPLET_IP:6080/vnc.html
```

- Enter your VNC password
- You should see a desktop with Chromium browser
- The browser will show Worn Wear pages during polling cycles

---

## Quick Commands

| Task | Command |
|------|---------|
| Restart all services | `systemctl restart xvfb x11vnc novnc wornwear-bot` |
| View bot logs | `journalctl -u wornwear-bot -f` |
| Check memory | `free -m` |
| Edit .env | `nano /root/wornwear-bot/.env` |
| Test ntfy | `curl -d "test" https://ntfy.sh/your-topic` |
| Check ports | `ss -tlnp \| grep -E '5900\|6080'` |

---

## What Changed from Old Version

### Old (headless-only):
- Bot ran headless (no visible browser)
- Notifications sent with just product link
- No way to manually complete checkout
- Required separate login/session management

### New (noVNC):
- Bot runs with visible browser in Xvfb
- Notifications include noVNC link
- Click link on phone → see live browser → manual checkout
- Simpler - no login needed, you do it manually

---

## Expected Flow When Item Drops

1. **Bot finds match** → Immediately bags item (30-min hold starts)
2. **You get ntfy notification**:
   ```
   🎯 Worn Wear — Item Bagged!

   Men's Retro-X Fleece Jacket — $89.00
   Matched: keywords ['fleece', 'jacket']
   https://wornwear.patagonia.com/products/...

   ✅ Item bagged! You have 30 minutes.

   🖥️ Complete checkout here:
   http://YOUR_IP:6080/vnc.html?autoconnect=true
   ```

3. **Tap the noVNC link** → Opens in phone browser
4. **Enter VNC password** → See the live Chromium browser
5. **Navigate to cart** → Review item photos/details
6. **Manually complete checkout** → Enter payment, place order
7. **25 minutes later** → Second notification reminds you (if you haven't finished)

---

## Security Notes

- **VNC password**: Stored in `/root/.vnc/passwd` (encrypted)
- **Port 6080**: Restrict to your IP in DigitalOcean firewall
- **No card storage**: Your payment details never touch the server
- **SSH key only**: Make sure password login is disabled

---

## Memory Usage

With noVNC setup (non-headless browser):

- **Idle**: ~300-400 MB
- **During polling**: ~500-600 MB
- **With swap**: Can handle spikes up to 1.5 GB

The 1GB swap file prevents crashes during checkout.

---

## Rollback to Headless (If Needed)

If you want to go back to simple headless mode:

```bash
# In .env
USE_VNC=false

# Restart bot (no need to stop xvfb/x11vnc/novnc)
systemctl restart wornwear-bot
```

The bot will run headless but xvfb/novnc will keep running harmlessly.

---

## Troubleshooting

**"VNC mode: disabled" in logs but USE_VNC=true in .env**
- Restart the bot: `systemctl restart wornwear-bot`
- Check .env has no quotes: `USE_VNC=true` not `USE_VNC="true"`

**noVNC shows black screen**
- Bot hasn't launched browser yet (between poll cycles)
- Wait 30-60 seconds for next cycle
- Check logs: `journalctl -u wornwear-bot -f`

**Notification has no noVNC link**
- Check `DROPLET_IP` is set in .env
- Check `USE_VNC=true` in .env
- Restart bot after changing .env

**Browser crashes during checkout**
- Check memory: `free -m`
- Verify swap is active: `swapon --show`
- Consider upgrading to 2GB droplet ($12/mo)

---

## Testing on Droplet

Once deployed, trigger a test match:

1. Set `KEYWORDS=fleece` (very common word)
2. Wait for next poll cycle (max 65 seconds)
3. Should immediately find matches and send notification
4. Tap noVNC link to verify browser access works
5. Clear cart and reset your real keywords

Or use the test script:

```bash
cd /root/wornwear-bot
python test_local.py --headless
```

(Test script works on droplet too, but you won't see the browser unless you use noVNC)
