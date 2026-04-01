"""
Worn Wear Monitor Bot
Polls wornwear.patagonia.com for items matching your keywords and/or style
numbers, then optionally adds to cart when found.

Includes:
  - Scroll-based "load more" to fetch the full catalog (not just first 24)
  - Style number matching (checks individual product pages for style #)
  - Per-click product harvesting to catch items that rotate OUT of the DOM
    once the ~577-item display cap is hit (confirmed via test_pagination.py)

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
import time

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
CLEAR_TIMESTAMP_FILE = "last_cleared.txt"
CLEAR_INTERVAL_HOURS = 24

# How long to wait after each scroll before checking if new products loaded
SCROLL_PAUSE_MS = 4000
# Give up scrolling after this many consecutive scrolls with no new products
SCROLL_MAX_STALE = 8

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

def should_clear_seen() -> bool:
    """Check if 24 hours have passed since last clear."""
    if not os.path.exists(CLEAR_TIMESTAMP_FILE):
        return True

    try:
        with open(CLEAR_TIMESTAMP_FILE) as f:
            last_cleared = float(f.read().strip())
        hours_since_clear = (time.time() - last_cleared) / 3600
        return hours_since_clear >= CLEAR_INTERVAL_HOURS
    except Exception:
        return True

def mark_cleared():
    """Record current time as last clear timestamp."""
    with open(CLEAR_TIMESTAMP_FILE, "w") as f:
        f.write(str(time.time()))

def load_seen() -> set:
    # Check if we should clear seen items (every 24 hours)
    if should_clear_seen():
        log.info(f"⏰ {CLEAR_INTERVAL_HOURS}h passed — clearing seen items to catch re-listed products")
        mark_cleared()
        return set()

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
        # Ensure strings are properly encoded as UTF-8
        body_bytes = body.encode('utf-8')
        headers = {
            "Title": title,
            "Priority": "high",
            "Tags": "shopping",
            "Content-Type": "text/plain; charset=utf-8",
        }
        async with httpx.AsyncClient() as client:
            await client.post(
                NOTIFY_URL,
                content=body_bytes,
                headers=headers,
                timeout=10,
            )
        log.info(f"Notification sent: {title}")
    except Exception as e:
        log.warning(f"Notification failed: {e}")


async def cart_expiry_warning(title: str, url: str, delay_seconds: int = 1500):
    """Fire a warning notification before the cart expires (default 25 min)."""
    await asyncio.sleep(delay_seconds)

    novnc_url = f"http://{DROPLET_IP}:6080/vnc.html?autoconnect=true" if DROPLET_IP and USE_VNC else ""

    warning_body = f"{title} - cart expires in 5 minutes!\n"
    if novnc_url:
        warning_body += f"\nCheckout now:\n{novnc_url}"
    else:
        warning_body += f"\n{url}"

    await notify(
        title="Cart expiring in 5 minutes!",
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


# ── JS snippets ───────────────────────────────────────────────────────────────

# Count of unique product URLs currently visible in the DOM
_COUNT_JS = """
    () => new Set(
        Array.from(document.querySelectorAll('a[href*="/products/"]'))
            .map(a => a.href)
    ).size
"""

# Extract every product visible in the DOM right now
_EXTRACT_JS = """
    () => {
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
"""


# ── Listing scraper with per-click harvesting ─────────────────────────────────

async def scrape_all_products(page: Page, url: str) -> list[dict]:
    """
    Navigate to a listing page, click Load More until the full catalog is
    exhausted, and return every unique product seen.

    WHY PER-CLICK HARVESTING:
    The Worn Wear site caps the visible DOM at ~577 items. Once that ceiling
    is hit, each Load More click rotates NEW products IN while pushing older
    ones OUT of the DOM. If we only read the DOM at the end of pagination we
    silently miss everything that has already been evicted. Testing confirmed
    77+ products were missed in a single session using the old approach.

    FIX: we snapshot the DOM at every Load More click and accumulate products
    into `all_products` (keyed by URL) throughout the session. Anything that
    disappears from the DOM mid-session is already safely stored.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(random.randint(2000, 3000))
    except Exception as e:
        log.warning(f"Failed to load {url}: {e}")
        return []

    # Accumulator: url → product dict. Persists across the whole session so
    # products evicted from the DOM are not lost.
    all_products: dict[str, dict] = {}

    async def harvest():
        """Snapshot current DOM products into all_products."""
        products = await page.evaluate(_EXTRACT_JS)
        new_count = 0
        for p in products:
            if p["url"] not in all_products:
                all_products[p["url"]] = p
                new_count += 1
        return new_count

    # Initial harvest before any clicking
    await harvest()

    stale_count  = 0
    last_count   = await page.evaluate(_COUNT_JS)
    scroll_round = 0

    LOAD_MORE_SELECTORS = [
        "button:has-text('Load More')",
        "button:has-text('Show More')",
        "button:has-text('View More')",
        "[class*='load-more']",
        "[class*='LoadMore']",
    ]

    # How many consecutive clicks with zero new *unique* products before we stop.
    # We use a separate stale counter for unique products so that DOM-capped
    # rotation clicks (DOM flat, but new uniques still appearing) keep running.
    unique_stale = 0
    UNIQUE_STALE_LIMIT = 3

    while stale_count < SCROLL_MAX_STALE:
        scroll_round += 1

        # Scroll to bottom (helps trigger lazy-load and brings the button into view)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(SCROLL_PAUSE_MS)

        # Try clicking a Load More button
        clicked = False
        for load_more_sel in LOAD_MORE_SELECTORS:
            try:
                btn = page.locator(load_more_sel).first
                if await btn.is_visible(timeout=800) and await btn.is_enabled(timeout=800):
                    log.info(f"  Clicking load-more button: {load_more_sel}")
                    await btn.click()
                    await page.wait_for_timeout(SCROLL_PAUSE_MS)
                    clicked = True
                    break
            except Exception:
                continue

        current_dom_count = await page.evaluate(_COUNT_JS)

        # ── Harvest after every click/scroll ──────────────────────────────────
        new_unique = await harvest()

        if current_dom_count > last_count:
            # DOM is still growing — normal phase (clicks 1-23 in testing)
            log.info(
                f"  Scroll {scroll_round}: DOM {last_count} -> {current_dom_count} "
                f"| total unique harvested: {len(all_products)} (+{new_unique} new)"
            )
            last_count   = current_dom_count
            stale_count  = 0
            unique_stale = 0

        elif new_unique > 0:
            # DOM is capped but new products are rotating in — keep going
            log.info(
                f"  Scroll {scroll_round}: DOM capped at {current_dom_count} "
                f"| +{new_unique} new unique | total harvested: {len(all_products)}"
            )
            stale_count  = 0   # don't stop just because the DOM count is flat
            unique_stale = 0

        else:
            # Neither DOM nor unique set grew
            stale_count  += 1
            unique_stale += 1
            log.info(
                f"  Scroll {scroll_round}: no growth "
                f"(dom stale {stale_count}/{SCROLL_MAX_STALE}, "
                f"unique stale {unique_stale}/{UNIQUE_STALE_LIMIT}) "
                f"| total harvested: {len(all_products)}"
            )
            if unique_stale >= UNIQUE_STALE_LIMIT:
                log.info("  Catalog fully exhausted — stopping pagination.")
                break

    log.info(
        f"  Pagination complete: {len(all_products)} unique products harvested "
        f"(DOM showed {current_dom_count} at end)"
    )

    return list(all_products.values())


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

            # Check if we should clear seen items (every 24 hours while running)
            if should_clear_seen():
                log.info(f"⏰ {CLEAR_INTERVAL_HOURS}h passed — clearing seen items to catch re-listed products")
                mark_cleared()
                seen.clear()  # Clear the in-memory set
                save_seen(seen)  # Persist the empty set

            for url in TARGET_URLS:
                log.info(f"Checking {url} …")
                products = await scrape_all_products(page, url)
                log.info(f"  Total unique products harvested: {len(products)}")

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
                        f"{title} - {product.get('price', '')}\n"
                        f"Matched: {', '.join(match_reason)}\n"
                        f"{product.get('url', '')}"
                    )

                    if AUTO_ADD_CART and product.get("url"):
                        # Bag the item first
                        success = await add_to_cart(page, product["url"])

                        if success:
                            notification_body += "\n\nItem bagged! You have 30 minutes."
                            if novnc_url:
                                notification_body += f"\n\nComplete checkout here:\n{novnc_url}"

                            await notify(
                                title="Worn Wear - Item Bagged!",
                                body=notification_body,
                            )

                            # Schedule 25-minute expiry warning
                            asyncio.create_task(cart_expiry_warning(title, product.get("url", "")))
                        else:
                            await notify(
                                title="Add to Cart Failed",
                                body=(
                                    f"Found {title} but couldn't add to cart - check logs.\n"
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