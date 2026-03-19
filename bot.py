"""
Worn Wear Monitor Bot
Polls wornwear.patagonia.com for items matching your keywords and/or style
numbers, then optionally adds to cart when found.

Includes:
  - Scroll-based "load more" to fetch the full catalog (not just first 24)
  - Style number matching (checks individual product pages for style #)

Requirements:
    uv sync
    uv run playwright install chromium

.env:
    KEYWORDS=synchilla,fleece,medium   # ALL must match listing title (comma-separated)
    STYLE_NUMBERS=25523,19975          # ANY match triggers alert (comma-separated, optional)
    POLL_MIN=30
    POLL_MAX=65
    AUTO_ADD_CART=true                 # Set to true to auto-bag items
    NOTIFY_URL=https://ntfy.sh/your-topic
    USE_VNC=false                      # Set to true on droplet for noVNC access
    DROPLET_IP=                        # Your droplet IP (for noVNC links)

Matching logic:
  - If KEYWORDS set and STYLE_NUMBERS set: alert if keywords match OR style # matches
  - If only KEYWORDS set:                  alert if keywords match
  - If only STYLE_NUMBERS set:             alert if style # matches
"""

import asyncio
import json
import logging
import os
import random
import re

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Page, async_playwright

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
import sys
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("wornwear-bot")

# ── Config ────────────────────────────────────────────────────────────────────
_raw_keywords     = os.getenv("KEYWORDS", "retro-x,vintage").strip()
_raw_styles       = os.getenv("STYLE_NUMBERS", "25528").strip()

KEYWORDS      = [k.strip() for k in _raw_keywords.split(",") if k.strip()] if _raw_keywords else []
STYLE_NUMBERS = [s.strip() for s in _raw_styles.split(",") if s.strip()]   if _raw_styles   else []

POLL_MIN      = int(os.getenv("POLL_MIN", "30"))
POLL_MAX      = int(os.getenv("POLL_MAX", "65"))
AUTO_ADD_CART = os.getenv("AUTO_ADD_CART", "false").lower() == "true"
NOTIFY_URL    = os.getenv("NOTIFY_URL", "")

# noVNC setup (set USE_VNC=true on production droplet)
USE_VNC       = os.getenv("USE_VNC", "false").lower() == "true"
DROPLET_IP    = os.getenv("DROPLET_IP", "")

STATE_FILE   = "seen_items.json"

# How long to wait after each scroll before checking if new products loaded
SCROLL_PAUSE_MS = 2500
# Give up scrolling after this many consecutive scrolls with no new products
SCROLL_MAX_STALE = 3

TARGET_URLS = [
    "https://wornwear.patagonia.com/collections/just-added",
]

# ── Add-to-cart selectors ─────────────────────────────────────────────────────
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


# ── State persistence ─────────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set):
    with open(STATE_FILE, "w") as f:
        json.dump(list(seen), f)


# ── Notifications ─────────────────────────────────────────────────────────────

async def notify(title: str, body: str):
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


async def cart_expiry_warning(title: str, url: str, delay_seconds: int = 1500):
    """Fire a warning notification before the cart expires (default 25 min)."""
    await asyncio.sleep(delay_seconds)

    novnc_url = f"http://{DROPLET_IP}:6080/vnc.html?autoconnect=true" if DROPLET_IP and USE_VNC else ""

    warning_body = f"{title} — cart expires in 5 minutes!\n"
    if novnc_url:
        warning_body += f"\n🖥️ Checkout now:\n{novnc_url}"
    else:
        warning_body += f"\n{url}"

    await notify(
        title="⚠️ Cart expiring in 5 minutes!",
        body=warning_body,
    )


# ── Matching logic ────────────────────────────────────────────────────────────

def keywords_match(title: str) -> bool:
    """All keywords must appear in the listing title."""
    if not KEYWORDS:
        return False
    t = title.lower()
    return all(kw.lower() in t for kw in KEYWORDS)

def style_number_match(product_url: str) -> tuple[bool, str]:
    """
    Extract the style number directly from the product URL — no page load needed.

    Worn Wear URLs follow the pattern:
        /products/mens-better-sweater-jacket_25528_sth
                                             ^^^^^
    The style number is the numeric segment between the two underscores.
    This makes style matching instant with zero extra HTTP requests.
    """
    if not STYLE_NUMBERS:
        return False, ""

    # e.g. "_25528_" -> "25528"
    segments = re.findall(r'_(\d{4,6})_', product_url)
    for seg in segments:
        if seg in STYLE_NUMBERS:
            return True, seg

    return False, ""


# ── Listing scraper with scroll-to-load-more ──────────────────────────────────

async def scrape_all_products(page: Page, url: str) -> list[dict]:
    """
    Navigate to a listing page and scroll until no new products load,
    collecting the full catalog rather than just the initial 24.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(random.randint(2000, 3000))
    except Exception as e:
        log.warning(f"Failed to load {url}: {e}")
        return []

    stale_count  = 0
    last_count   = 0
    scroll_round = 0

    while stale_count < SCROLL_MAX_STALE:
        scroll_round += 1

        # Scroll to the very bottom of the page
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(SCROLL_PAUSE_MS)

        # Also try clicking a "Load More" button if one exists
        for load_more_sel in [
            "button:has-text('Load More')",
            "button:has-text('Show More')",
            "button:has-text('View More')",
            "[class*='load-more']",
            "[class*='LoadMore']",
        ]:
            try:
                btn = page.locator(load_more_sel).first
                if await btn.is_visible(timeout=800) and await btn.is_enabled(timeout=800):
                    log.info(f"  Clicking load-more button: {load_more_sel}")
                    await btn.click()
                    await page.wait_for_timeout(SCROLL_PAUSE_MS)
                    break
            except Exception:
                continue

        # Count how many product links are currently in the DOM
        current_count = await page.evaluate("""
            () => new Set(
                Array.from(document.querySelectorAll('a[href*="/products/"]'))
                    .map(a => a.href)
            ).size
        """)

        if current_count > last_count:
            log.info(f"  Scroll {scroll_round}: {last_count} -> {current_count} products")
            last_count  = current_count
            stale_count = 0
        else:
            stale_count += 1
            log.info(
                f"  Scroll {scroll_round}: still {current_count} products "
                f"(stale {stale_count}/{SCROLL_MAX_STALE})"
            )

    log.info(f"  Pagination complete: {last_count} unique product URLs found")

    # Extract full product list now that everything is loaded
    products = await page.evaluate("""
        () => {
            // Deduplicate by href
            const seen = new Set();
            const links = Array.from(document.querySelectorAll('a[href*="/products/"]'))
                .filter(a => {
                    if (seen.has(a.href)) return false;
                    seen.add(a.href);
                    return true;
                });

            return links.map(link => {
                const parent = link.closest(
                    'div[class*="product"], div[class*="card"], li, article'
                ) || link.parentElement;
                const priceEl = parent?.querySelector(
                    '[class*="price"], .price, [class*="Price"]'
                );
                return {
                    title: link.innerText?.trim() ||
                           link.querySelector('h1,h2,h3,h4,p')?.innerText?.trim() || '',
                    price: priceEl?.innerText?.trim() || '',
                    url:   link.href,
                    id:    link.href.split('/').pop()
                };
            }).filter(p => p.title && p.url);
        }
    """)

    return products


# ── Add to cart ───────────────────────────────────────────────────────────────

async def add_to_cart(page: Page, product_url: str) -> bool:
    try:
        log.info(f"Attempting add-to-cart: {product_url}")
        await page.goto(product_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(random.randint(1500, 3000))

        size_clicked = False
        try:
            size_inputs = await page.evaluate("""
                () => Array.from(
                    document.querySelectorAll('input[name="size-option"]')
                ).map(i => ({ value: i.value, id: i.id }))
            """)
            log.info(f"Size options: {[s['value'] for s in size_inputs]}")

            for size in size_inputs:
                label = page.locator(f"label[for='{size['id']}']").first
                try:
                    await label.click(timeout=2000)
                    await page.wait_for_timeout(800)
                    is_checked = await page.evaluate(
                        f"() => document.getElementById('{size['id']}').checked"
                    )
                    if is_checked:
                        log.info(f"Selected size: '{size['value']}'")
                        size_clicked = True
                        break
                except Exception:
                    continue
        except Exception as e:
            log.warning(f"Size selection error: {e}")

        if not size_clicked:
            log.warning("Could not select any size — may all be sold out.")

        for sel in ADD_TO_CART_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500) and await btn.is_enabled(timeout=1500):
                    log.info(f"Clicking add-to-cart via: {sel}")
                    await btn.click()
                    await page.wait_for_timeout(2500)
                    return True
            except Exception:
                continue

        log.warning("No visible/enabled Add to Cart button found.")
        return False

    except Exception as e:
        log.error(f"Add to cart failed: {e}")
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run():
    seen = load_seen()

    log.info(f"Bot started.")
    log.info(f"  Keywords:     {KEYWORDS or '(none)'}")
    log.info(f"  Style #s:     {STYLE_NUMBERS or '(none)'}")
    log.info(f"  Poll interval: {POLL_MIN}–{POLL_MAX}s  |  Auto-cart: {AUTO_ADD_CART}")
    log.info(f"  VNC mode:     {'enabled' if USE_VNC else 'disabled (local/headless)'}")
    log.info(f"  Seen items:   {len(seen)} from previous runs")

    if not KEYWORDS and not STYLE_NUMBERS:
        log.error(
            "No KEYWORDS or STYLE_NUMBERS set in .env — nothing to search for. Exiting."
        )
        return

    async with async_playwright() as pw:
        # Configure browser based on VNC mode
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        if USE_VNC:
            launch_args.append("--display=:99")

        browser = await pw.chromium.launch(
            headless=not USE_VNC,  # headless=False when VNC is enabled
            args=launch_args,
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

        poll_count = 0

        # ── Poll loop ─────────────────────────────────────────────────────────
        while True:
            poll_count += 1

            for url in TARGET_URLS:
                log.info(f"Checking {url} …")
                products = await scrape_all_products(page, url)
                log.info(f"  Total products after full scroll: {len(products)}")

                for product in products:
                    pid   = product.get("id") or product.get("url")
                    title = product.get("title", "")

                    if not pid or pid in seen:
                        continue

                    # ── Keyword match (fast — no extra page load) ─────────────
                    kw_hit = keywords_match(title)

                    # ── Style number match (instant — read from URL, no page load) ──
                    style_hit  = False
                    found_style = ""
                    if STYLE_NUMBERS and not kw_hit:
                        style_hit, found_style = style_number_match(product["url"])

                    if not kw_hit and not style_hit:
                        continue

                    # ── We have a match ───────────────────────────────────────
                    match_reason = []
                    if kw_hit:
                        match_reason.append(f"keywords {KEYWORDS}")
                    if style_hit:
                        match_reason.append(f"style #{found_style}")

                    log.info(f"  MATCH ({', '.join(match_reason)}): {title}  ({product.get('price', '?')})")
                    log.info(f"  URL: {product.get('url')}")

                    # Build notification with noVNC link if VNC is enabled
                    novnc_url = f"http://{DROPLET_IP}:6080/vnc.html?autoconnect=true" if DROPLET_IP and USE_VNC else ""

                    notification_body = (
                        f"{title} — {product.get('price', '')}\n"
                        f"Matched: {', '.join(match_reason)}\n"
                        f"{product.get('url', '')}"
                    )

                    if AUTO_ADD_CART and product.get("url"):
                        # Bag the item first
                        success = await add_to_cart(page, product["url"])

                        if success:
                            notification_body += "\n\n✅ Item bagged! You have 30 minutes."
                            if novnc_url:
                                notification_body += f"\n\n🖥️ Complete checkout here:\n{novnc_url}"

                            await notify(
                                title="🎯 Worn Wear — Item Bagged!",
                                body=notification_body,
                            )

                            # Schedule 25-minute expiry warning
                            asyncio.create_task(cart_expiry_warning(title, product.get("url", "")))
                        else:
                            await notify(
                                title="⚠️ Add to Cart Failed",
                                body=(
                                    f"Found {title} but couldn't add to cart — check logs.\n"
                                    f"{product.get('url', '')}"
                                ),
                            )
                    else:
                        # Just notify without bagging
                        await notify(
                            title="Worn Wear Match Found!",
                            body=notification_body,
                        )

                    seen.add(pid)
                    save_seen(seen)

            delay = random.uniform(POLL_MIN, POLL_MAX)
            log.info(f"Sleeping {delay:.0f}s …\n")
            await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(run())