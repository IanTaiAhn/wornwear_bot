# Local Testing Guide

Test the bot locally before deploying to your droplet.

---

## Quick Start

### 1. Set up your `.env` file

```bash
# Required for testing
NOTIFY_URL=https://ntfy.sh/your-unique-topic
AUTO_ADD_CART=true

# Optional - test keyword matching
KEYWORDS=fleece,jacket
STYLE_NUMBERS=25528

# Leave these as false for local testing
USE_VNC=false
DROPLET_IP=

# Polling (not used in test script, but needed for bot.py)
POLL_MIN=30
POLL_MAX=65
```

### 2. Run the test script

```bash
# Watch the browser in action (recommended for first run)
python test_local.py

# Or run headless (faster)
python test_local.py --headless
```

### 3. What the test does

1. **Adds a real item to cart** - Uses a test product URL from Worn Wear
2. **Sends notification** - You'll get an ntfy notification on your phone
3. **Tests expiry warning** - After 10 seconds, sends the "5 minutes left" warning
4. **Keeps browser open** - So you can inspect the cart/checkout

---

## Expected Output

```
============================================================
WORN WEAR BOT — LOCAL TEST
============================================================
  Headless mode:  False
  VNC enabled:    False
  Notify URL:     https://ntfy.sh/your-topic
  Droplet IP:     (not set)
============================================================

[1/3] Adding test item to cart: https://wornwear.patagonia.com/...
  Attempting add-to-cart: https://wornwear.patagonia.com/...
  Size options: ['XS', 'S', 'M', 'L', 'XL']
  Selected size: 'XS'
  Clicking add-to-cart via: button:has-text('Add to Cart')
✅ Item added to cart successfully!

[2/3] Sending notification...
  Notification sent: 🧪 TEST — Worn Wear Item Bagged!
✅ Notification sent!
   Check your ntfy app: https://ntfy.sh/your-topic

[3/3] Testing cart expiry warning...
   (Normally fires after 25 min, but we'll use 10 seconds for testing)
   Waiting 10 seconds for expiry warning to fire...

  Notification sent: ⚠️ Cart expiring in 5 minutes!
✅ Expiry warning sent!

============================================================
TEST COMPLETE
============================================================
```

---

## Troubleshooting

**"Add to cart failed"**
- The product might be sold out or removed
- Try a different product URL from wornwear.patagonia.com
- Update the `test_product` URL in `test_local.py`

**"Notification sent but I didn't get it"**
- Check your `NOTIFY_URL` is correct
- Open the URL in a browser to subscribe to that topic
- Make sure you have the ntfy app installed or have the web page open

**Browser crashes or hangs**
- Run with `--headless` flag to reduce memory usage
- Close other applications to free up RAM

**Size selection fails**
- The product might only have one size or no sizes
- This is normal - the bot will try to add anyway

---

## Testing VNC Mode (Advanced)

To test the full noVNC setup locally (requires Linux/WSL):

1. Install Xvfb locally:
   ```bash
   # Ubuntu/Debian
   sudo apt install xvfb
   ```

2. Start Xvfb:
   ```bash
   Xvfb :99 -screen 0 1280x800x24 &
   ```

3. Update `.env`:
   ```bash
   USE_VNC=true
   DROPLET_IP=localhost
   ```

4. Run test:
   ```bash
   DISPLAY=:99 python test_local.py
   ```

This is optional - you'll do the real VNC setup on the droplet anyway.

---

## After Testing

Before running the real bot:

1. **Clear your cart**: https://wornwear.patagonia.com/cart
2. **Check your keywords** are correct in `.env`
3. **Set realistic poll intervals** (POLL_MIN/POLL_MAX)
4. **Deploy to droplet** and set `USE_VNC=true` there

---

## Real Bot vs Test Script

| Feature | Test Script | Real Bot |
|---------|-------------|----------|
| Polls listings | ❌ No | ✅ Yes |
| Keyword matching | ❌ Skipped | ✅ Yes |
| Add to cart | ✅ Yes | ✅ Yes |
| Notifications | ✅ Yes | ✅ Yes |
| 25-min warning | ✅ 10sec test | ✅ 25min real |
| Seen items tracking | ❌ No | ✅ Yes |

The test script just validates that add-to-cart and notifications work.
The real bot will continuously poll and match based on your keywords.
