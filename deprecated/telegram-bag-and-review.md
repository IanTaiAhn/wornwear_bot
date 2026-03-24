# LOSER OPTION FOR NOW

# Worn Wear — Bag, Review & Buy Implementation Guide

Auto-bags matching items to hold them, sends you a Telegram message with
photos and buttons to approve or skip, then either completes checkout or
clears the cart based on your response.

---

## How It Works

```
Bot finds match
    ↓
Immediately adds to cart (starts 30-min hold)
    ↓
Telegram message fires with item photo + buttons:
    [📸 View Item]  [✅ Buy It]  [❌ Skip]
    ↓
You tap Buy It → bot completes guest checkout
You tap Skip   → bot removes item from cart
No response in 25 min → bot auto-removes + notifies you
```

---

## What You'll Need

- A Telegram account
- A Telegram bot token (free, takes 2 minutes)
- Your Telegram chat ID (easy to get)
- Your payment + shipping details stored in `.env`
- The existing bot running on your DigitalOcean droplet

---

## Part 1 — Create Your Telegram Bot

### 1a. Get a bot token

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Follow the prompts — pick any name and username
4. BotFather replies with a token like:
   ```
   1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
   ```
5. Copy it — this is your `TELEGRAM_BOT_TOKEN`

### 1b. Get your chat ID

1. Search for **@userinfobot** in Telegram
2. Send it any message
3. It replies with your user ID — a number like `987654321`
4. This is your `TELEGRAM_CHAT_ID`

---

## Part 2 — Update .env

Add these to your `.env` file on the droplet:

```bash
# Telegram
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_CHAT_ID=987654321

# Guest checkout details (used when you tap Buy It)
CHECKOUT_EMAIL=your@email.com
CHECKOUT_FIRST_NAME=John
CHECKOUT_LAST_NAME=Smith
CHECKOUT_ADDRESS=123 Main St
CHECKOUT_CITY=Salt Lake City
CHECKOUT_STATE=UT
CHECKOUT_ZIP=84101
CHECKOUT_COUNTRY=US
CHECKOUT_PHONE=5551234567

# Card details (stored only on your private droplet)
CHECKOUT_CARD_NUMBER=4111111111111111
CHECKOUT_CARD_EXPIRY_MONTH=12
CHECKOUT_CARD_EXPIRY_YEAR=2027
CHECKOUT_CARD_CVV=123
```

> Your `.env` is in `.gitignore` and never leaves the droplet. Still, treat
> your droplet like your wallet — make sure SSH is key-based and root login
> is locked down.

---

## Part 3 — Install Dependencies

On the droplet:

```bash
cd /root/wornwear-bot
uv add python-telegram-bot
```

---

## Part 4 — New File: telegram_handler.py

Create a new file `/root/wornwear-bot/telegram_handler.py`:

```python
"""
telegram_handler.py

Handles all Telegram bot interactions:
  - Sends match notifications with inline Buy / Skip buttons
  - Listens for button presses
  - Triggers checkout or cart removal based on response
  - Auto-removes cart items after 25 minutes with no response
"""

import asyncio
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

log = logging.getLogger("telegram-handler")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

# Seconds before auto-removing the item if you don't respond (25 min)
CART_TIMEOUT_SECONDS = 1500

# Filled in by bot.py when a match is found
# Maps callback_data key → { product, page, remove_fn, checkout_fn }
_pending: dict[str, dict] = {}


def _make_app() -> Application:
    return Application.builder().token(TELEGRAM_BOT_TOKEN).build()


async def send_match_notification(
    product: dict,
    on_buy,       # async callable: completes checkout
    on_skip,      # async callable: removes item from cart
) -> None:
    """
    Send an inline keyboard message for a matched product.
    Registers buy/skip callbacks and schedules auto-removal.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping notification")
        return

    pid = product.get("id", product.get("url", "unknown"))

    # Register callbacks
    _pending[f"buy_{pid}"]  = {"product": product, "fn": on_buy,  "done": False}
    _pending[f"skip_{pid}"] = {"product": product, "fn": on_skip, "done": False}

    title = product.get("title", "Unknown item")
    price = product.get("price", "?")
    url   = product.get("url", "")

    message = (
        f"🎯 *Item Bagged!*\n\n"
        f"*{title}*\n"
        f"Price: {price}\n"
        f"[View on Worn Wear]({url})\n\n"
        f"⏳ Cart hold expires in 30 minutes.\n"
        f"You have 25 minutes to decide."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Buy It",   callback_data=f"buy_{pid}"),
            InlineKeyboardButton("❌ Skip It",  callback_data=f"skip_{pid}"),
        ],
        [
            InlineKeyboardButton("📸 View Item", url=url),
        ],
    ])

    app = _make_app()
    async with app:
        msg = await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=False,  # shows link preview with photo
        )

    log.info(f"Telegram notification sent for: {title}")

    # Schedule auto-removal after 25 minutes
    asyncio.create_task(
        _auto_remove(pid, product, on_skip, msg.message_id)
    )


async def _auto_remove(pid: str, product: dict, on_skip, message_id: int):
    """Remove the item from cart if no response after CART_TIMEOUT_SECONDS."""
    await asyncio.sleep(CART_TIMEOUT_SECONDS)

    buy_entry  = _pending.get(f"buy_{pid}")
    skip_entry = _pending.get(f"skip_{pid}")

    # Already handled by user
    if buy_entry and buy_entry.get("done"):
        return
    if skip_entry and skip_entry.get("done"):
        return

    log.info(f"No response in 25 min — auto-removing cart item: {product.get('title')}")

    try:
        await on_skip()
    except Exception as e:
        log.error(f"Auto-remove failed: {e}")

    # Mark as done
    for key in [f"buy_{pid}", f"skip_{pid}"]:
        if key in _pending:
            _pending[key]["done"] = True

    # Update the Telegram message to show it expired
    try:
        app = _make_app()
        async with app:
            await app.bot.edit_message_text(
                chat_id=TELEGRAM_CHAT_ID,
                message_id=message_id,
                text=(
                    f"⏰ *Cart Expired*\n\n"
                    f"*{product.get('title')}* was removed from your cart "
                    f"after 25 minutes with no response."
                ),
                parse_mode="Markdown",
            )
    except Exception as e:
        log.warning(f"Could not update expired message: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Buy It / Skip It button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data  # e.g. "buy_some-product-slug"

    entry = _pending.get(data)
    if not entry:
        await query.edit_message_text("⚠️ This item is no longer pending.")
        return

    if entry.get("done"):
        await query.edit_message_text("✅ Already handled.")
        return

    entry["done"] = True
    product = entry["product"]
    title   = product.get("title", "Item")

    if data.startswith("buy_"):
        # Mark the skip as done too so auto-remove doesn't fire
        pid = data[len("buy_"):]
        if f"skip_{pid}" in _pending:
            _pending[f"skip_{pid}"]["done"] = True

        await query.edit_message_text(
            f"⏳ *Completing checkout for:*\n{title}\n\nStand by…",
            parse_mode="Markdown",
        )
        try:
            success = await entry["fn"]()
            if success:
                await context.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=f"🎉 *Order placed!*\n\n{title} is on its way.",
                    parse_mode="Markdown",
                )
            else:
                await context.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=(
                        f"⚠️ *Checkout failed* for {title}.\n"
                        f"Check the bot logs — you may need to complete it manually.\n"
                        f"{product.get('url', '')}"
                    ),
                    parse_mode="Markdown",
                )
        except Exception as e:
            log.error(f"Checkout error: {e}")
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"❌ Checkout error: {e}",
            )

    elif data.startswith("skip_"):
        pid = data[len("skip_"):]
        if f"buy_{pid}" in _pending:
            _pending[f"buy_{pid}"]["done"] = True

        await query.edit_message_text(
            f"🗑️ *Skipped* — removing {title} from cart…",
            parse_mode="Markdown",
        )
        try:
            await entry["fn"]()
            await query.edit_message_text(
                f"🗑️ *Skipped*\n\n{title} removed from cart.",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error(f"Cart removal error: {e}")


async def start_polling():
    """
    Start the Telegram bot polling loop.
    Run this as a background asyncio task alongside the main bot loop.
    """
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set — Telegram handler not started")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("Telegram bot polling started")
    async with app:
        await app.start()
        await app.updater.start_polling()
        # Run forever (cancelled when the main bot shuts down)
        await asyncio.Event().wait()
```

---

## Part 5 — New File: checkout.py

Create `/root/wornwear-bot/checkout.py`:

```python
"""
checkout.py

Completes guest checkout on Worn Wear using stored credentials.
Called when the user taps Buy It in Telegram.
"""

import logging
import os
import random

from playwright.async_api import Page

log = logging.getLogger("checkout")

# Load from .env
CHECKOUT = {
    "email":        os.getenv("CHECKOUT_EMAIL", ""),
    "first_name":   os.getenv("CHECKOUT_FIRST_NAME", ""),
    "last_name":    os.getenv("CHECKOUT_LAST_NAME", ""),
    "address":      os.getenv("CHECKOUT_ADDRESS", ""),
    "city":         os.getenv("CHECKOUT_CITY", ""),
    "state":        os.getenv("CHECKOUT_STATE", ""),
    "zip":          os.getenv("CHECKOUT_ZIP", ""),
    "country":      os.getenv("CHECKOUT_COUNTRY", "US"),
    "phone":        os.getenv("CHECKOUT_PHONE", ""),
    "card_number":  os.getenv("CHECKOUT_CARD_NUMBER", ""),
    "card_month":   os.getenv("CHECKOUT_CARD_EXPIRY_MONTH", ""),
    "card_year":    os.getenv("CHECKOUT_CARD_EXPIRY_YEAR", ""),
    "card_cvv":     os.getenv("CHECKOUT_CARD_CVV", ""),
}

CART_URL     = "https://wornwear.patagonia.com/cart"
CHECKOUT_URL = "https://wornwear.patagonia.com/checkout"


async def remove_from_cart(page: Page) -> bool:
    """
    Navigate to the cart and remove all items.
    Returns True if cart was successfully cleared.
    """
    try:
        log.info("Navigating to cart to remove item...")
        await page.goto(CART_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        # Try common remove button selectors
        remove_selectors = [
            "button[aria-label*='Remove']",
            "button[aria-label*='remove']",
            "a[href*='/cart/change']",
            "[class*='remove']",
            "[class*='Remove']",
            "button:has-text('Remove')",
            ".icon-close",
        ]

        removed = False
        for _ in range(10):  # max 10 items
            clicked = False
            for sel in remove_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=1500):
                        await btn.click()
                        await page.wait_for_timeout(1500)
                        clicked = True
                        removed = True
                        log.info(f"  Removed item via: {sel}")
                        break
                except Exception:
                    continue
            if not clicked:
                break  # No more remove buttons found

        if removed:
            log.info("Cart cleared successfully")
        else:
            log.warning("No remove buttons found — cart may already be empty")

        return True

    except Exception as e:
        log.error(f"Cart removal failed: {e}")
        return False


async def complete_checkout(page: Page) -> bool:
    """
    Complete guest checkout using credentials from .env.
    Returns True if order was placed successfully.

    NOTE: Shopify checkout flows vary. If this fails, run with headless=False
    and watch the browser to identify which step is breaking, then adjust
    the selectors below.
    """
    try:
        log.info("Starting checkout flow...")
        await page.goto(CHECKOUT_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(random.randint(2000, 3000))

        # ── Step 1: Contact / Email ───────────────────────────────────────────
        log.info("Filling contact info...")
        for sel in ["#email", "input[name='email']", "input[type='email']"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.fill(CHECKOUT["email"])
                    break
            except Exception:
                continue

        # ── Step 2: Shipping address ──────────────────────────────────────────
        log.info("Filling shipping address...")
        field_map = {
            "#TextField0, input[name='firstName'], #firstName": CHECKOUT["first_name"],
            "#TextField1, input[name='lastName'], #lastName":   CHECKOUT["last_name"],
            "#TextField3, input[name='address1'], #address1":   CHECKOUT["address"],
            "#TextField5, input[name='city'], #city":           CHECKOUT["city"],
            "#TextField7, input[name='postalCode'], #zip":      CHECKOUT["zip"],
            "input[name='phone'], #phone":                      CHECKOUT["phone"],
        }

        for selectors, value in field_map.items():
            for sel in selectors.split(", "):
                try:
                    el = page.locator(sel.strip()).first
                    if await el.is_visible(timeout=1500):
                        await el.fill(value)
                        break
                except Exception:
                    continue

        # State/province dropdown
        try:
            state_sel = page.locator("select[name='zone'], #Select1").first
            if await state_sel.is_visible(timeout=1500):
                await state_sel.select_option(value=CHECKOUT["state"])
        except Exception:
            pass

        # Continue to shipping method
        await _click_continue(page, "Continue to shipping")
        await page.wait_for_timeout(2000)

        # ── Step 3: Shipping method — pick first available ────────────────────
        log.info("Selecting shipping method...")
        try:
            shipping_option = page.locator("input[name='shipping_rate']").first
            if await shipping_option.is_visible(timeout=3000):
                await shipping_option.click()
        except Exception:
            pass  # May auto-select

        await _click_continue(page, "Continue to payment")
        await page.wait_for_timeout(2000)

        # ── Step 4: Payment ───────────────────────────────────────────────────
        log.info("Filling payment info...")

        # Card number is often in an iframe on Shopify
        try:
            card_frame = page.frame_locator(
                "iframe[title*='Card number'], iframe[src*='card-fields']"
            ).first
            await card_frame.locator("input").fill(CHECKOUT["card_number"])
        except Exception:
            # Fallback: try direct input
            for sel in ["#number", "input[name='number']", "[placeholder*='card number' i]"]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=1500):
                        await el.fill(CHECKOUT["card_number"])
                        break
                except Exception:
                    continue

        # Expiry
        expiry_str = f"{CHECKOUT['card_month']}/{CHECKOUT['card_year'][-2:]}"
        for sel in ["#expiry", "input[name='expiry']", "[placeholder*='expir' i]"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1500):
                    await el.fill(expiry_str)
                    break
            except Exception:
                continue

        # CVV
        for sel in ["#verification_value", "input[name='verification_value']", "[placeholder*='CVV' i]", "[placeholder*='CVC' i]"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1500):
                    await el.fill(CHECKOUT["card_cvv"])
                    break
            except Exception:
                continue

        # ── Step 5: Place order ───────────────────────────────────────────────
        log.info("Placing order...")
        placed = await _click_continue(page, "Pay now", "Place order", "Complete order")
        await page.wait_for_timeout(5000)

        # Check for confirmation
        url = page.url
        if "thank_you" in url or "order-confirmed" in url or "confirmation" in url.lower():
            log.info("✅ Order placed successfully!")
            return True

        # Look for confirmation text on page
        for confirmation_text in ["Thank you", "Order confirmed", "Your order"]:
            try:
                if await page.locator(f"text={confirmation_text}").first.is_visible(timeout=2000):
                    log.info("✅ Order confirmed via page text")
                    return True
            except Exception:
                continue

        log.warning("Could not confirm order placement — check logs and browser state")
        return False

    except Exception as e:
        log.error(f"Checkout failed: {e}")
        return False


async def _click_continue(page: Page, *button_texts: str) -> bool:
    """Click a continue/submit button, trying multiple text variants."""
    selectors = [f"button:has-text('{t}')" for t in button_texts] + [
        "button[type='submit']",
        "input[type='submit']",
        "[data-testid*='submit']",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000) and await btn.is_enabled(timeout=2000):
                await btn.click()
                return True
        except Exception:
            continue
    log.warning(f"Could not find continue button (tried: {button_texts})")
    return False
```

---

## Part 6 — Update bot.py

### 6a. Add new imports at the top

```python
from telegram_handler import send_match_notification, start_polling
from checkout import complete_checkout, remove_from_cart
```

Remove the old `notify()` import/usage for match alerts — Telegram replaces it.
You can keep ntfy for non-match alerts like errors if you want.

### 6b. Start the Telegram polling loop

In the `run()` function, right after the browser/context is created:

```python
# Start Telegram bot in the background
asyncio.create_task(start_polling())
```

### 6c. Replace the match handling block

Find the section that starts with `if not kw_hit and not style_hit: continue`
and replace the notification + cart block with:

```python
if not kw_hit and not style_hit:
    continue

# ── Match found ───────────────────────────────────────────────────────────────
match_reason = []
if kw_hit:   match_reason.append(f"keywords {KEYWORDS}")
if style_hit: match_reason.append(f"style #{found_style}")

log.info(f"  MATCH ({', '.join(match_reason)}): {title}  ({product.get('price', '?')})")
log.info(f"  URL: {product.get('url')}")

# Step 1: Bag the item immediately
if product.get("url"):
    bagged = await add_to_cart(page, product["url"])
    if not bagged:
        log.warning("Could not bag item — skipping notification")
        continue
    log.info("  Item bagged — 30-minute hold started")

# Step 2: Define buy / skip callbacks for the Telegram buttons
# Capture page and product in closures
async def on_buy(p=page, prod=product):
    return await complete_checkout(p)

async def on_skip(p=page, prod=product):
    return await remove_from_cart(p)

# Step 3: Send Telegram notification with inline buttons
await send_match_notification(
    product=product,
    on_buy=on_buy,
    on_skip=on_skip,
)

seen.add(pid)
save_seen(seen)
```

---

## Part 7 — Deploy to the Droplet

```bash
# Upload the two new files from your local machine
scp checkout.py telegram_handler.py root@YOUR_DROPLET_IP:/root/wornwear-bot/

# SSH in and install the new dependency
ssh root@YOUR_DROPLET_IP
cd /root/wornwear-bot
uv add python-telegram-bot

# Update your .env with Telegram + checkout credentials
nano .env

# Restart the bot
systemctl restart wornwear-bot

# Watch the logs to confirm it starts cleanly
journalctl -u wornwear-bot -f
```

---

## Part 8 — Test It End to End

Before relying on this in the wild, do a dry run:

### Test Telegram notifications

Add a quick test script `test_telegram.py`:

```python
import asyncio, os
from dotenv import load_dotenv
load_dotenv()

from telegram_handler import send_match_notification

async def test():
    fake_product = {
        "id":    "test-item-001",
        "title": "Men's Retro-X Fleece Jacket — Large — Black",
        "price": "$89.00",
        "url":   "https://wornwear.patagonia.com/products/test",
    }
    async def fake_buy():
        print("BUY tapped!")
        return True
    async def fake_skip():
        print("SKIP tapped!")
        return True

    await send_match_notification(fake_product, fake_buy, fake_skip)
    print("Notification sent — tap the buttons in Telegram")
    await asyncio.sleep(60)  # wait for button press

asyncio.run(test())
```

```bash
uv run python test_telegram.py
```

Check Telegram — you should get the message. Tap each button and confirm the
console prints the right response.

### Test cart removal

```bash
# Manually add something to cart first via test_add_to_cart.py, then:
uv run python -c "
import asyncio
from playwright.async_api import async_playwright
from checkout import remove_from_cart

async def test():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        result = await remove_from_cart(page)
        print('Removed:', result)
        await browser.close()

asyncio.run(test())
"
```

---

## How the Checkout Selectors May Need Tuning

Shopify checkout flows are mostly standardized but Worn Wear may have
customizations. If `complete_checkout()` fails:

1. Temporarily set `headless=False` in bot.py
2. Trigger a test purchase via `test_telegram.py` and tap Buy It
3. Watch the browser — identify which field or button isn't being found
4. Check the element in browser DevTools and update the selector in `checkout.py`

The most common failure points are:

- **Card number iframe** — Shopify loads card fields in a sandboxed iframe
- **State dropdown** — may use a custom component instead of a native `<select>`
- **Continue buttons** — text varies ("Pay now" vs "Place order" vs "Complete order")

---

## Useful Commands Reference

| Task                | Command                          |
| ------------------- | -------------------------------- |
| Watch live bot logs | `journalctl -u wornwear-bot -f`  |
| Restart bot         | `systemctl restart wornwear-bot` |
| Test Telegram only  | `uv run python test_telegram.py` |
| Check memory        | `free -m`                        |
| Edit credentials    | `nano /root/wornwear-bot/.env`   |

---

## Security Notes

- Your card details live only in `.env` on the droplet — never committed to git
- Ensure your droplet has SSH key auth and password login disabled
- Restrict DigitalOcean firewall to only the ports you need (22 for SSH)
- Consider rotating your card details after a big purchase season
