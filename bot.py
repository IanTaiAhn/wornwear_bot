"""
Worn Wear Monitor Bot
Polls wornwear.patagonia.com for items matching your keywords,
then optionally adds to cart when found.

Requirements:
    uv sync
    uv run playwright install
"""

import asyncio
import random
import json
import os
import time
import logging
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)
log = logging.getLogger("wornwear-bot")

# ── Config (edit these or put them in a .env file) ────────────────────────────
KEYWORDS        = os.getenv("KEYWORDS", "hoody").split(",")
POLL_MIN        = int(os.getenv("POLL_MIN", "30"))       # seconds
POLL_MAX        = int(os.getenv("POLL_MAX", "65"))       # seconds
AUTO_ADD_CART   = os.getenv("AUTO_ADD_CART", "false").lower() == "true"
NOTIFY_URL      = "https://ntfy.sh/wornwear_bot"
# NOTIFY_URL      = os.getenv("NOTIFY_URL", "")           # ntfy.sh or similar
STATE_FILE      = "seen_items.json"

# All known add-to-cart button selectors — extend if Patagonia updates their markup
ADD_TO_CART_SELECTORS = [
    "button[data-testid='add-to-cart']",
    "button:has-text('Add to Cart')",
    "button:has-text('Add to Bag')",
    "button:has-text('Add To Cart')",
    "#add-to-cart",
    ".add-to-cart",
    "[class*='AddToCart']",
    "[class*='add-to-cart']",
]

# Pages to monitor — add more as needed
TARGET_URLS = [
    "https://wornwear.patagonia.com/collections/mens-fleece",
]

# ── State persistence ─────────────────────────────────────────────────────────
def load_seen() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set):
    with open(STATE_FILE, "w") as f:
        json.dump(list(seen), f)

# ── Notification ──────────────────────────────────────────────────────────────
async def notify(title: str, body: str):
    """
    Sends a push notification via ntfy.sh (free, no account needed).
    Set NOTIFY_URL=https://ntfy.sh/your-topic-name in .env
    """
    if not NOTIFY_URL:
        return
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(
                NOTIFY_URL,
                content=body,
                headers={"Title": title, "Priority": "high", "Tags": "shopping"},
                timeout=10,
            )
        log.info(f"Notification sent: {title}")
    except Exception as e:
        log.warning(f"Notification failed: {e}")

# ── Core scraping ─────────────────────────────────────────────────────────────
def matches_keywords(text: str, keywords: list[str]) -> bool:
    text = text.lower()
    return all(kw.strip().lower() in text for kw in keywords)

async def scrape_page(page, url: str) -> list[dict]:
    """Navigate to a listing page and return all products found."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Give JS a moment to render products
        await page.wait_for_timeout(random.randint(2000, 4000))

        # Worn Wear is a React app — products render into the DOM as cards.
        # We find products by looking for links to /products/ URLs
        products = await page.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a[href*="/products/"]'));

                return links.map(link => {
                    const parent = link.closest('div[class*="product"], div[class*="card"], li, article') || link.parentElement;

                    // Try to find price in parent element
                    const priceEl = parent.querySelector('[class*="price"], .price, [class*="Price"]');

                    return {
                        title: link.innerText?.trim() || link.querySelector('h1, h2, h3, h4, p')?.innerText?.trim() || '',
                        price: priceEl?.innerText?.trim() || '',
                        url: link.href,
                        id: link.href.split('/').pop()
                    };
                }).filter(p => p.title && p.url);
            }
        """)
        return [p for p in products if p.get("title")]
    except Exception as e:
        log.warning(f"Failed to scrape {url}: {e}")
        return []

# ── Add to cart ───────────────────────────────────────────────────────────────
async def add_to_cart(page, product_url: str) -> bool:
    """
    Navigate to a product page, select an available size, then click Add to Cart.
    Returns True on success.
    """
    try:
        log.info(f"Attempting to add to cart: {product_url}")
        await page.goto(product_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(random.randint(1500, 3000))

        title = await page.title()
        log.info(f"Product page: {title}")

        # ── Step 1: select an available size ─────────────────────────────────
        size_clicked = False
        try:
            size_inputs = await page.evaluate("""
                () => {
                    const inputs = Array.from(document.querySelectorAll(
                        'input[name="size-option"]'
                    ));
                    return inputs.map(input => ({
                        value: input.value,
                        id:    input.id,
                    }));
                }
            """)

            log.info(f"Found {len(size_inputs)} size options: {[s['value'] for s in size_inputs]}")

            for size in size_inputs:
                label = page.locator(f"label[for='{size['id']}']").first
                try:
                    await label.click(timeout=2000)
                    await page.wait_for_timeout(800)

                    # Confirm the radio is actually checked
                    is_checked = await page.evaluate(
                        f"() => document.getElementById('{size['id']}').checked"
                    )
                    if is_checked:
                        log.info(f"Selected size: '{size['value']}'")
                        size_clicked = True
                        break
                    else:
                        log.info(f"  '{size['value']}' click did not register, trying next")
                except Exception:
                    log.info(f"  '{size['value']}' not clickable (likely sold out), trying next")

        except Exception as e:
            log.warning(f"Size selection error: {e}")

        if not size_clicked:
            log.warning("Could not select any available size — all may be sold out.")

        # ── Step 2: click Add to Cart ─────────────────────────────────────────
        for sel in ADD_TO_CART_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500) and await btn.is_enabled(timeout=1500):
                    log.info(f"Found add-to-cart button via: {sel}")
                    await btn.click()
                    await page.wait_for_timeout(2500)

                    # Check for cart confirmation signals in the page
                    cart_signals = await page.evaluate("""
                        () => {
                            const signals = [
                                document.querySelector('[class*="cart"][class*="count"]'),
                                document.querySelector('[data-testid*="cart"]'),
                                document.querySelector('[aria-label*="cart"]'),
                                document.querySelector('[class*="CartCount"]'),
                            ];
                            return signals
                                .filter(Boolean)
                                .map(el => ({ tag: el.tagName, text: el.innerText?.trim(), class: el.className }));
                        }
                    """)

                    if cart_signals:
                        log.info(f"Cart confirmation detected: {cart_signals}")
                    else:
                        log.info("Add to Cart clicked (no cart badge found to confirm, but button was clicked).")

                    return True
            except Exception:
                continue

        log.warning("Could not find a visible, enabled Add to Cart button.")
        log.warning("If this keeps failing, inspect the product page in DevTools and")
        log.warning("add the button's selector to ADD_TO_CART_SELECTORS in bot.py.")
        return False

    except Exception as e:
        log.error(f"Add to cart failed: {e}")
        return False

# ── Main loop ─────────────────────────────────────────────────────────────────
async def run():
    seen = load_seen()
    log.info(f"Bot started. Keywords: {KEYWORDS}")
    log.info(f"Poll interval: {POLL_MIN}–{POLL_MAX}s  |  Auto-cart: {AUTO_ADD_CART}")
    log.info(f"Already seen {len(seen)} items from previous runs.")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = await context.new_page()

        while True:
            for url in TARGET_URLS:
                log.info(f"Checking {url} …")
                products = await scrape_page(page, url)
                log.info(f"  Found {len(products)} products on page")

                for product in products:
                    pid   = product.get("id") or product.get("url")
                    title = product.get("title", "")

                    if not pid or pid in seen:
                        continue  # already processed

                    if matches_keywords(title, KEYWORDS):
                        log.info(f"    MATCH: {title}  ({product.get('price', '?')})")
                        log.info(f"    URL: {product.get('url')}")

                        await notify(
                            title="Worn Wear Match Found!",
                            body=f"{title} — {product.get('price','')}\n{product.get('url','')}",
                        )

                        if AUTO_ADD_CART and product.get("url"):
                            success = await add_to_cart(page, product["url"])
                            if success:
                                await notify(
                                    title="Added to Cart!",
                                    body=f"{title} was added to your cart.\n{product.get('url','')}",
                                )
                            else:
                                await notify(
                                    title="Add to Cart Failed",
                                    body=f"Found {title} but couldn't add to cart — check logs.\n{product.get('url','')}",
                                )

                        seen.add(pid)
                        save_seen(seen)

            # Randomized sleep between poll cycles
            delay = random.uniform(POLL_MIN, POLL_MAX)
            log.info(f"Sleeping {delay:.0f}s …\n")
            await asyncio.sleep(delay)

if __name__ == "__main__":
    asyncio.run(run())