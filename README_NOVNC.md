# Worn Wear Bot — noVNC Integration

Your bot now supports remote browser access via noVNC, allowing you to manually complete checkout from your phone after items are automatically bagged.

---

## 📋 What Changed

### New Features
- ✅ **Remote browser access** via noVNC (tap link on phone → see live Chromium)
- ✅ **Manual checkout** - Full control over payment entry and purchase decision
- ✅ **25-minute expiry warning** - Second notification before cart expires
- ✅ **Local/Production modes** - Test locally (headless), deploy with VNC

### Configuration
Two new environment variables in `.env`:

```bash
USE_VNC=false       # Set to true on droplet
DROPLET_IP=         # Your droplet IP for noVNC links
```

---

## 🚀 Quick Start

### Local Testing (Before Droplet)

1. **Copy and configure .env**:
   ```bash
   cp .env.example .env
   nano .env
   ```

   Set these:
   ```bash
   KEYWORDS=fleece,jacket
   STYLE_NUMBERS=25528
   AUTO_ADD_CART=true
   NOTIFY_URL=https://ntfy.sh/your-topic
   USE_VNC=false
   DROPLET_IP=
   ```

2. **Run test script**:
   ```bash
   python test_local.py
   ```

3. **Check results**:
   - Browser adds item to cart
   - You get ntfy notification on phone
   - After 10 seconds, expiry warning arrives

4. **Clear cart** before running real bot:
   https://wornwear.patagonia.com/cart

**Full local testing guide**: See `TESTING.md`

---

### Production Deployment (Droplet)

1. **Update `.env` on droplet**:
   ```bash
   USE_VNC=true
   DROPLET_IP=123.456.789.012  # Your actual IP
   AUTO_ADD_CART=true
   ```

2. **Set up noVNC stack**:
   - Follow **all steps** in `novnc-implementation.md`
   - Install Xvfb, x11vnc, noVNC
   - Create 4 systemd services
   - Open port 6080 in firewall

3. **Deploy updated bot.py**:
   ```bash
   scp bot.py root@YOUR_IP:/root/wornwear-bot/
   ```

4. **Restart services**:
   ```bash
   systemctl restart wornwear-bot
   journalctl -u wornwear-bot -f
   ```

**Full deployment guide**: See `DROPLET_SETUP.md`

---

## 📱 Usage Flow

### When a match is found:

1. **Bot bags item** (happens instantly, 30-min hold starts)

2. **You get notification**:
   ```
   🎯 Worn Wear — Item Bagged!

   Men's Retro-X Fleece — $89.00
   Matched: keywords ['fleece', 'jacket']

   ✅ Item bagged! You have 30 minutes.

   🖥️ Complete checkout here:
   http://123.456.789.012:6080/vnc.html?autoconnect=true
   ```

3. **Tap the link** → Opens noVNC in phone browser

4. **Enter VNC password** → See live Chromium browser

5. **Navigate to cart** → Review item photos/condition

6. **Manually checkout** → Enter payment, place order

7. **25 minutes later** → Reminder notification if still pending

---

## 🗂️ File Guide

| File | Purpose |
|------|---------|
| `bot.py` | Main bot (updated with VNC support) |
| `test_local.py` | Local testing script |
| `TESTING.md` | Local testing guide |
| `DROPLET_SETUP.md` | Droplet deployment guide |
| `novnc-implementation.md` | Full noVNC setup instructions |
| `.env.example` | Sample configuration |

---

## 🔧 Configuration Reference

### .env Variables

| Variable | Local | Droplet | Purpose |
|----------|-------|---------|---------|
| `KEYWORDS` | ✅ | ✅ | Keywords to match (ALL must appear) |
| `STYLE_NUMBERS` | ✅ | ✅ | Style numbers to match (ANY can match) |
| `AUTO_ADD_CART` | `true` | `true` | Auto-bag items when matched |
| `NOTIFY_URL` | ✅ | ✅ | ntfy.sh notification URL |
| `USE_VNC` | `false` | `true` | Enable noVNC remote access |
| `DROPLET_IP` | empty | `123.456.789.012` | For noVNC links |
| `POLL_MIN` | `30` | `30` | Min seconds between polls |
| `POLL_MAX` | `65` | `65` | Max seconds between polls |

---

## 🛡️ Security

### What's Secure
- ✅ No card details stored on server
- ✅ VNC password protected
- ✅ Port 6080 restricted to your IP
- ✅ Manual payment entry only

### Best Practices
- Restrict firewall to your IP only (not 0.0.0.0/0)
- Use strong VNC password
- SSH key authentication (no password login)
- Keep `.env` in `.gitignore` (never commit)

---

## 🐛 Troubleshooting

### Local Testing Issues

**"Add to cart failed"**
- Product might be sold out
- Try different URL in test script

**"No notification received"**
- Check `NOTIFY_URL` is set
- Open URL in browser to subscribe
- Install ntfy app on phone

### Droplet Issues

**"VNC mode: disabled" in logs**
- Check `.env` has `USE_VNC=true`
- No quotes: `USE_VNC=true` not `USE_VNC="true"`
- Restart: `systemctl restart wornwear-bot`

**noVNC shows black screen**
- Bot hasn't started polling yet (wait 30-60s)
- Check logs: `journalctl -u wornwear-bot -f`

**No noVNC link in notification**
- Check `DROPLET_IP` is set in `.env`
- Check `USE_VNC=true`
- Restart bot after editing `.env`

**Browser crashes during checkout**
- Check memory: `free -m`
- Verify swap: `swapon --show`
- Add 1GB swap (see `novnc-implementation.md` Step 1)

---

## 📊 Comparison: Old vs New

| Feature | Old (Headless Only) | New (noVNC) |
|---------|---------------------|-------------|
| Item bagging | ✅ Auto | ✅ Auto |
| Checkout | ❌ Not supported | ✅ Manual via phone |
| Payment storage | N/A | ✅ Not needed |
| Browser visibility | ❌ Hidden | ✅ Visible via VNC |
| Memory usage | ~200MB | ~500MB (with swap) |
| Notification type | Basic link | Link + noVNC URL |
| Mobile access | ❌ No | ✅ Yes |

---

## 🎯 Next Steps

1. ✅ **Test locally** - Run `python test_local.py` and verify notifications work
2. ✅ **Clear cart** - Remove test items before deploying
3. ✅ **Deploy to droplet** - Follow `DROPLET_SETUP.md` for full setup
4. ✅ **Test noVNC** - Open `http://YOUR_IP:6080/vnc.html` and verify browser access
5. ✅ **Set real keywords** - Update `.env` with your actual search terms
6. ✅ **Monitor logs** - Watch `journalctl -u wornwear-bot -f` for first few cycles

---

## 📚 Documentation

- **Local Testing**: `TESTING.md`
- **Droplet Setup**: `DROPLET_SETUP.md`
- **noVNC Full Guide**: `novnc-implementation.md`
- **Telegram Alternative**: `telegram-bag-and-review.md` (not recommended - auto-purchases)

---

## ❓ FAQ

**Q: Do I need to set up noVNC to run the bot?**
A: No - you can run it headless (USE_VNC=false) and just get notifications. But you won't be able to complete checkout.

**Q: Can I test noVNC locally?**
A: Yes, on Linux/WSL with Xvfb installed. See `TESTING.md` "Testing VNC Mode" section.

**Q: What if I want auto-checkout instead?**
A: See `telegram-bag-and-review.md` - but this requires storing card details and gives you no manual review.

**Q: How much does the droplet cost?**
A: $6/month (1GB RAM) with noVNC should work. Upgrade to $12/month (2GB) if you see crashes.

**Q: Is this secure?**
A: Yes - VNC is password protected, port is firewalled to your IP, and your card details never touch the server.

---

**Need help?** Check the logs first: `journalctl -u wornwear-bot -f`
