"""
Worn Wear Monitor Bot
Polls wornwear.patagonia.com for items matching your keywords and/or style
numbers, then optionally adds to cart when found.

Runs two concurrent loops in one browser (see run_general_loop/run_grail_loop):
  - General loop: broad keyword/style search across TARGET_URLS, full
    catalog scroll-pagination, current POLL_MIN-MAX cadence.
  - Grail loop: tight-interval watcher for the rare style numbers in
    rare_items.json, via narrow single-item searches (?q=<style>) that
    don't need pagination — so it never gets stuck behind the general
    loop's slow full-catalog scrape.

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

    # Cart tab (noVNC only) — all optional, shown with defaults
    SHOW_CART_TAB=true                 # Pin a tab on /cart so it's always visible
    CART_REFRESH_SECONDS=20            # How often the cart tab reloads
    CART_REFOCUS_SECONDS=4             # How often it steals focus back from other tabs

    # Grail loop (rare_items.json watcher) — all optional, shown with defaults
    GRAIL_POLL_MIN=10
    GRAIL_POLL_MAX=20
    GRAIL_TABS=3                       # concurrent tabs checking rare style numbers
    GRAIL_COOLDOWN_SECONDS=1800        # skip re-attempts on a confirmed bag for this long
    GRAIL_RETRY_COOLDOWN_SECONDS=120   # skip re-attempts on a failed attempt for this long

Matching logic:
  - If KEYWORDS set and STYLE_NUMBERS set: alert if keywords match OR style # matches
  - If only KEYWORDS set:                  alert if keywords match
  - If only STYLE_NUMBERS set:             alert if style # matches
  - rare_items.json entries are always watched by the grail loop regardless
    of KEYWORDS/STYLE_NUMBERS.
"""

import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
import random
import re
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Page, async_playwright

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
import sys
from logging.handlers import RotatingFileHandler

# Set up rotating log handler: max 10 MB per file, keep 3 backups (30 MB total max)
file_handler = RotatingFileHandler(
    "bot.log",
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=3,               # Keep bot.log.1, bot.log.2, bot.log.3
    encoding="utf-8",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)),
        file_handler,
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

# Grail loop — a second, much tighter poll loop that watches only the rare
# style numbers in rare_items.json via narrow single-item searches, running
# concurrently with the general loop in the same browser (own tab(s), so it
# never gets stuck behind the general loop's slow full-catalog scrape).
GRAIL_POLL_MIN               = int(os.getenv("GRAIL_POLL_MIN", "10"))
GRAIL_POLL_MAX               = int(os.getenv("GRAIL_POLL_MAX", "20"))
GRAIL_TABS                   = int(os.getenv("GRAIL_TABS", "1"))
GRAIL_COOLDOWN_SECONDS       = int(os.getenv("GRAIL_COOLDOWN_SECONDS", "1800"))  # after a confirmed bag (~cart hold length)
GRAIL_RETRY_COOLDOWN_SECONDS = int(os.getenv("GRAIL_RETRY_COOLDOWN_SECONDS", "120"))  # after a failed/no-op attempt

# noVNC setup (set USE_VNC=true on production droplet)
USE_VNC       = os.getenv("USE_VNC", "false").lower() == "true"
DROPLET_IP    = os.getenv("DROPLET_IP", "")

# Cart tab — a dedicated tab pinned to /cart so it's always visible over
# noVNC, instead of whatever page the scraping/grail tabs last navigated to.
# Only meaningful when USE_VNC is on (there's no display to show it otherwise).
CART_URL              = "https://wornwear.patagonia.com/cart"
SHOW_CART_TAB         = os.getenv("SHOW_CART_TAB", "true").lower() == "true"
CART_REFRESH_SECONDS  = int(os.getenv("CART_REFRESH_SECONDS", "300"))  # reload cadence
CART_REFOCUS_SECONDS  = int(os.getenv("CART_REFOCUS_SECONDS", "300"))   # re-focus cadence

# Active hours — bot only runs between ACTIVE_START and ACTIVE_END (America/Denver)
ACTIVE_TZ    = ZoneInfo("America/Denver")
ACTIVE_START = int(os.getenv("ACTIVE_START", "7"))   # 7 AM
ACTIVE_END   = int(os.getenv("ACTIVE_END",   "23"))  # 11 PM

STATE_FILE        = "seen_items.json"
RARE_ITEMS_FILE   = "rare_items.json"
CLEAR_TIMESTAMP_FILE = "last_cleared.txt"
CLEAR_INTERVAL_HOURS = 999999  # Never clear - prevents re-bagging vintage items

# How long to wait after each scroll before checking if new products loaded
SCROLL_PAUSE_MS = 4000
# Give up scrolling after this many consecutive scrolls with no new products
SCROLL_MAX_STALE = 8

TARGET_URLS = [
    # ── Jackets ────────────────────────────────────────────────────────────────
    "https://wornwear.patagonia.com/search?q=vintage+jacket+men",

    # ── Shirts ─────────────────────────────────────────────────────────────────
    "https://wornwear.patagonia.com/search?q=vintage+shirt+men",
    "https://wornwear.patagonia.com/search?q=vintage+flannel+men",
    "https://wornwear.patagonia.com/search?q=chamois+shirt+men",
    "https://wornwear.patagonia.com/search?q=retro+shirt+men",

    # ── Pants ──────────────────────────────────────────────────────────────────
    "https://wornwear.patagonia.com/search?q=vintage+pants+men",
    "https://wornwear.patagonia.com/search?q=stand+up+pants+men",
    "https://wornwear.patagonia.com/search?q=iron+forge+pants+men",
    "https://wornwear.patagonia.com/search?q=retro+pants+men",

    # ── Shorts ─────────────────────────────────────────────────────────────────
    "https://wornwear.patagonia.com/search?q=vintage+shorts+men",
    "https://wornwear.patagonia.com/search?q=baggies+shorts+men",
    "https://wornwear.patagonia.com/search?q=stand+up+shorts+men",
    "https://wornwear.patagonia.com/search?q=retro+shorts+men",

    # ── Hats ───────────────────────────────────────────────────────────────────
    "https://wornwear.patagonia.com/search?q=vintage+hat+men",
    "https://wornwear.patagonia.com/search?q=trucker+hat",
    "https://wornwear.patagonia.com/search?q=lopro+trucker",
    "https://wornwear.patagonia.com/search?q=p-6+hat",

    # ── Belts ──────────────────────────────────────────────────────────────────
    "https://wornwear.patagonia.com/search?q=vintage+belt+men",
    "https://wornwear.patagonia.com/search?q=web+belt",
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


# ── Rare-items list ──────────────────────────────────────────────────────────

def load_rare_items() -> dict:
    """Load rare_items.json, returning empty structure if missing or invalid."""
    if not os.path.exists(RARE_ITEMS_FILE):
        return {"style_numbers": [], "url_patterns": []}
    try:
        with open(RARE_ITEMS_FILE) as f:
            data = json.load(f)
        return {
            "style_numbers": [str(s).lower() for s in data.get("style_numbers", [])],
            "url_patterns":  [str(p).lower() for p in data.get("url_patterns",  [])],
        }
    except Exception as e:
        log.warning(f"Could not load {RARE_ITEMS_FILE}: {e}")
        return {"style_numbers": [], "url_patterns": []}


_RARE: dict = {}  # loaded once at startup, reloaded each poll


def is_rare_item(product_url: str) -> bool:
    """Return True if the item matches any entry in rare_items.json."""
    url_lower = product_url.lower()

    for sn in _RARE.get("style_numbers", []):
        if f"_{sn}_" in url_lower or url_lower.endswith(f"_{sn}"):
            return True

    for pat in _RARE.get("url_patterns", []):
        if pat.startswith("*") and pat.endswith("*"):
            if pat[1:-1] in url_lower:
                return True
        elif pat in url_lower:
            return True

    return False


def grail_search_urls() -> list[str]:
    """
    One narrow search URL per rare style number, derived directly from
    rare_items.json — no separate URL list to hand-maintain and keep in
    sync. Entries like "23055_vintage" carry a "_vintage" suffix used only
    for matching product URLs (see is_rare_item); as a *search query* only
    the numeric prefix is meaningful, so that's all that's used here.
    """
    queries: list[str] = []
    seen_q: set[str] = set()
    for sn in _RARE.get("style_numbers", []):
        q = sn.split("_")[0].strip()
        if q and q not in seen_q:
            seen_q.add(q)
            queries.append(f"https://wornwear.patagonia.com/search?q={quote(q)}")
    return queries


# ── Grail loop cooldown ───────────────────────────────────────────────────────
# The grail loop intentionally re-checks the same style numbers on every
# cycle (that's the point — catch a relist within seconds). Without a
# cooldown it would re-attempt add_to_cart on the same still-listed item
# every GRAIL_POLL_MIN-MAX seconds forever. This tracks, per product id, how
# long to leave it alone after an attempt.

_grail_cooldown_until: dict[str, float] = {}


def grail_on_cooldown(pid: str) -> bool:
    until = _grail_cooldown_until.get(pid)
    return until is not None and time.time() < until


def grail_start_cooldown(pid: str, seconds: int):
    _grail_cooldown_until[pid] = time.time() + seconds


# ── Active-hours gate ────────────────────────────────────────────────────────

def seconds_until_active() -> float:
    """Return 0 if now is within the active window, else seconds until it opens."""
    now = datetime.now(ACTIVE_TZ)
    if ACTIVE_START <= now.hour < ACTIVE_END:
        return 0.0
    next_open = now.replace(hour=ACTIVE_START, minute=0, second=0, microsecond=0)
    if now.hour >= ACTIVE_END:
        next_open += timedelta(days=1)
    return (next_open - now).total_seconds()


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


# ── Cart tab (noVNC) ─────────────────────────────────────────────────────────
# A dedicated page pinned to /cart so a human watching over noVNC always has
# a live, correct view of what's actually bagged — instead of whatever page
# a scraping/grail tab last navigated to.

_cart_page: Page | None = None


async def refresh_cart_view(reload: bool = True) -> None:
    """Bring the pinned cart tab back to front, optionally reloading it first
    so it reflects the latest state. No-op if the cart tab isn't enabled."""
    if _cart_page is None:
        return
    try:
        if reload:
            await _cart_page.reload(wait_until="domcontentloaded", timeout=15_000)
        await _cart_page.bring_to_front()
    except Exception as e:
        log.warning(f"Cart tab refresh failed: {e}")


async def cart_viewer_loop(cart_page: Page):
    """Keep the cart tab visible and current. Other tabs performing searches
    or add-to-cart clicks can steal window focus mid-poll; this periodically
    snaps focus back to the cart tab and reloads it on a slower cadence."""
    last_reload = time.monotonic()
    try:
        while True:
            await asyncio.sleep(CART_REFOCUS_SECONDS)
            due_for_reload = (time.monotonic() - last_reload) >= CART_REFRESH_SECONDS
            if due_for_reload:
                last_reload = time.monotonic()
            await refresh_cart_view(reload=due_for_reload)
    finally:
        await cart_page.close()


# ── Matching logic ────────────────────────────────────────────────────────────

def keywords_match(title: str) -> bool:
    """All keywords must appear in the listing title."""
    if not KEYWORDS:
        return False
    t = title.lower()
    return all(kw.lower() in t for kw in KEYWORDS)

def style_number_match(product_url: str) -> tuple[bool, str]:
    """
    Match style numbers against the product URL — no page load needed.

    Worn Wear URLs follow the pattern:
        /products/mens-retro-pile-fleece_10948_vintage_stone-heather

    Entry formats supported in STYLE_NUMBERS:
      - Contains wildcard (e.g. "*vintage*") — matches if that word appears
        anywhere in the URL, case-insensitive. Catches _vintage_, vintage-white,
        or any other variation regardless of position.
      - Prefix wildcard (e.g. "*_vintage") — matches any numeric style with
        that exact variant segment, e.g. _10291_vintage_, _10948_vintage_
      - Exact style+variant (e.g. "10948_vintage") — matches only that combo
      - Pure numeric (e.g. "25528") — matches _25528_ with any variant
    """
    if not STYLE_NUMBERS:
        return False, ""

    url_lower = product_url.lower()

    for style in STYLE_NUMBERS:
        if style.startswith("*") and style.endswith("*"):
            # Contains wildcard: "vintage" anywhere in the URL
            keyword = style[1:-1].lower()
            if keyword in url_lower:
                return True, style
        elif style.startswith("*_"):
            # Prefix wildcard: any numeric style with this variant segment
            variant = re.escape(style[2:])
            m = re.search(rf'_(\d{{4,6}}_{variant})_', product_url, re.IGNORECASE)
            if m:
                return True, m.group(1)
        elif re.fullmatch(r'\d{4,6}', style):
            # Pure numeric
            if re.search(rf'_{re.escape(style)}_', product_url):
                return True, style
        else:
            # Exact style+variant (e.g. "10948_vintage")
            if f"_{style.lower()}_" in url_lower:
                return True, style

    return False, ""


# ── JS snippets ───────────────────────────────────────────────────────────────

# Count of unique product URLs currently visible in the DOM (strip query params)
_COUNT_JS = """
    () => new Set(
        Array.from(document.querySelectorAll('a[href*="/products/"]'))
            .map(a => a.origin + new URL(a.href).pathname)
    ).size
"""

# Extract every product visible in the DOM right now
_EXTRACT_JS = """
    () => {
        const seen = new Set();
        const links = Array.from(document.querySelectorAll('a[href*="/products/"]'))
            .filter(a => {
                const key = a.origin + new URL(a.href).pathname;
                if (seen.has(key)) return false;
                seen.add(key);
                return true;
            });
        return links.map(link => {
            const parent = link.closest(
                'div[class*="product"], div[class*="card"], li, article'
            ) || link.parentElement;
            const priceEl = parent?.querySelector(
                '[class*="price"], .price, [class*="Price"]'
            );
            const cleanUrl = link.origin + new URL(link.href).pathname;
            return {
                title: link.innerText?.trim() ||
                       link.querySelector('h1,h2,h3,h4,p')?.innerText?.trim() || '',
                price: priceEl?.innerText?.trim() || '',
                url:   cleanUrl,
                id:    cleanUrl.split('/').pop()
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
        await page.goto(url, wait_until="networkidle", timeout=90_000)
        await page.wait_for_timeout(random.randint(5000, 8000))
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


async def scrape_grail_page(page: Page, url: str) -> list[dict]:
    """
    Lightweight, single-shot harvest for narrow style-number searches.

    A search like ?q=25410 returns only a handful of results that are all
    present in the DOM on first load — there's nothing to scroll or
    Load-More through. Unlike scrape_all_products (which always pays a
    ~15s+ floor waiting out stale scroll rounds, even on a near-empty
    results page) this is a single page load and one DOM read, ~1-2s.
    """
    try:
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(random.randint(3000, 5000))
        return await page.evaluate(_EXTRACT_JS)
    except Exception as e:
        log.warning(f"[grail] Failed to check {url}: {e}")
        return []


# ── Add to cart ───────────────────────────────────────────────────────────────

CART_COUNT_SELECTORS = [
    "[data-cart-count]",
    "[data-testid='cart-count']",
    ".cart-count",
    "[class*='cart-count']",
    "[class*='CartCount']",
    "[aria-label*='cart' i] [class*='count']",
]


async def _get_cart_count(page: Page) -> int | None:
    """Best-effort read of the header cart badge count. Returns None if it can't be determined."""
    for sel in CART_COUNT_SELECTORS:
        try:
            locator = page.locator(sel).first
            if await locator.count() == 0:
                continue
            text = (await locator.inner_text(timeout=1000)).strip()
            digits = "".join(ch for ch in text if ch.isdigit())
            if digits:
                return int(digits)
        except Exception:
            continue
    return None


async def add_to_cart(page: Page, product_url: str) -> bool:
    try:
        log.info(f"Attempting add-to-cart: {product_url}")
        await page.goto(product_url, wait_until="networkidle", timeout=90_000)
        await page.wait_for_timeout(random.randint(4000, 6000))

        # Get all available color options
        color_inputs = []
        try:
            color_inputs = await page.evaluate("""
                () => Array.from(
                    document.querySelectorAll('input[name="color-option"], input[type="radio"][class*="color"], [data-color-option]')
                ).map(i => ({ value: i.value, id: i.id, name: i.getAttribute('data-color') || i.value }))
            """)
            if color_inputs:
                log.info(f"Color options: {[c['name'] for c in color_inputs]}")
            else:
                log.info("No color options found - item may have single color")
        except Exception as e:
            log.warning(f"Color detection error: {e}")

        # Get all available size options
        size_inputs = []
        try:
            size_inputs = await page.evaluate("""
                () => Array.from(
                    document.querySelectorAll('input[name="size-option"]')
                ).map(i => ({ value: i.value, id: i.id }))
            """)
            if size_inputs:
                log.info(f"Size options: {[s['value'] for s in size_inputs]}")
            else:
                log.info("No size options found - item may be one-size")
        except Exception as e:
            log.warning(f"Size detection error: {e}")

        # If no colors found, treat as single color (proceed with sizes only)
        if not color_inputs:
            color_inputs = [{"value": "default", "id": None, "name": "default"}]

        # If no sizes found, treat as one-size (proceed with colors only)
        if not size_inputs:
            size_inputs = [{"value": "one-size", "id": None}]

        selection_success = False

        # Cycle through colors first, then sizes for each color
        for color in color_inputs:
            if color['id']:  # Only try to click if there's an actual color option
                try:
                    color_label = page.locator(f"label[for='{color['id']}']").first
                    await color_label.click(timeout=2000)
                    await page.wait_for_timeout(500)
                    log.info(f"Trying color: '{color['name']}'")
                except Exception as e:
                    log.warning(f"Failed to select color '{color['name']}': {e}")
                    continue

            # For this color, try each size
            for size in size_inputs:
                if size['id']:  # Only try to click if there's an actual size option
                    try:
                        size_label = page.locator(f"label[for='{size['id']}']").first
                        await size_label.click(timeout=2000)
                        await page.wait_for_timeout(800)
                        is_checked = await page.evaluate(
                            f"() => document.getElementById('{size['id']}').checked"
                        )
                        if is_checked:
                            log.info(f"Selected color: '{color['name']}', size: '{size['value']}'")
                            selection_success = True
                            break
                    except Exception as e:
                        log.warning(f"Failed to select size '{size['value']}' for color '{color['name']}': {e}")
                        continue
                else:
                    # No size selection needed (one-size item)
                    selection_success = True
                    break

            if selection_success:
                break

        if not selection_success:
            log.warning(
                "Could not select any valid color/size combination — may all be sold out. "
                "Aborting add-to-cart (no variant available to bag)."
            )
            return False

        cart_count_before = await _get_cart_count(page)

        for sel in ADD_TO_CART_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500) and await btn.is_enabled(timeout=1500):
                    log.info(f"Clicking add-to-cart via: {sel}")
                    await btn.click()
                    await page.wait_for_timeout(3000)

                    # Verify the item was actually added by checking for success indicators
                    success_indicators = [
                        "text=Added to cart",
                        "text=Added to bag",
                        "text=Item added",
                        "[class*='cart-success']",
                        "[class*='added-confirmation']",
                    ]

                    for indicator in success_indicators:
                        try:
                            if await page.locator(indicator).is_visible(timeout=2000):
                                log.info(f"Verified: item added successfully (found: {indicator})")
                                return True
                        except Exception:
                            continue

                    # Check for error messages that indicate failure
                    error_indicators = [
                        "text=Out of stock",
                        "text=Sold out",
                        "text=Not available",
                        "text=Select a size",
                        "[class*='error']",
                    ]

                    for error in error_indicators:
                        try:
                            if await page.locator(error).is_visible(timeout=1000):
                                log.warning(f"Add to cart failed: {error} message detected")
                                return False
                        except Exception:
                            continue

                    # No clear text-based indicator either way — fall back to comparing
                    # the header cart count before/after the click.
                    cart_count_after = await _get_cart_count(page)
                    if (
                        cart_count_before is not None
                        and cart_count_after is not None
                        and cart_count_after > cart_count_before
                    ):
                        log.info(
                            f"Verified: cart count increased ({cart_count_before} -> {cart_count_after})"
                        )
                        return True

                    # Still ambiguous — do NOT assume success. A false "bagged" report is
                    # far worse than a false failure for a bot bagging real, paid items.
                    log.warning(
                        "Ambiguous add-to-cart result — no success/error indicator and cart "
                        "count did not increase. Treating as failure to avoid a false 'bagged' report."
                    )
                    return False
            except Exception as e:
                log.warning(f"Exception clicking add-to-cart with {sel}: {e}")
                continue

        log.warning("No visible/enabled Add to Cart button found.")
        return False

    except Exception as e:
        log.error(f"Add to cart failed: {e}")
        return False


# ── Shared match handling ────────────────────────────────────────────────────

async def bag_and_notify(page: Page, product: dict, match_reason: list[str]) -> bool:
    """
    Handle a matched product: bag it (if AUTO_ADD_CART) and send the
    appropriate notification. Shared by both the general and grail loops so
    the bagging/notification behavior can't drift between them.

    Returns True only if the item was actually confirmed bagged.
    """
    title = product.get("title", "")
    url = product.get("url", "")
    novnc_url = f"http://{DROPLET_IP}:6080/vnc.html?autoconnect=true" if DROPLET_IP and USE_VNC else ""

    notification_body = (
        f"{title} - {product.get('price', '')}\n"
        f"Matched: {', '.join(match_reason)}\n"
        f"{url}"
    )

    if not (AUTO_ADD_CART and url):
        await notify(title="Worn Wear Match Found!", body=notification_body)
        return False

    success = await add_to_cart(page, url)

    if success:
        notification_body += "\n\nItem bagged! You have 30 minutes."
        if novnc_url:
            notification_body += f"\n\nComplete checkout here:\n{novnc_url}"

        await notify(title="Worn Wear - Item Bagged!", body=notification_body)
        await refresh_cart_view()

        # Schedule 25-minute expiry warning
        asyncio.create_task(cart_expiry_warning(title, url))
    else:
        await notify(
            title="Add to Cart Failed",
            body=f"Found {title} but couldn't add to cart - check logs.\n{url}",
        )

    return success


# ── General loop (broad keyword/style search) ───────────────────────────────

async def run_general_loop(context: BrowserContext):
    if not KEYWORDS and not STYLE_NUMBERS:
        log.info(
            "[general] No KEYWORDS or STYLE_NUMBERS set — general loop disabled "
            "(grail loop still runs if rare_items.json has entries)."
        )
        return

    seen = load_seen()

    log.info(f"[general] Keywords:      {KEYWORDS or '(none)'}")
    log.info(f"[general] Style #s:      {STYLE_NUMBERS or '(none)'}")
    log.info(f"[general] Poll interval: {POLL_MIN}–{POLL_MAX}s  |  Auto-cart: {AUTO_ADD_CART}")
    log.info(f"[general] Seen items:    {len(seen)} from previous runs")

    poll_count = 0

    while True:
        wait = seconds_until_active()
        if wait > 0:
            wake_at = datetime.now(ACTIVE_TZ) + timedelta(seconds=wait)
            log.info(
                f"[general] Outside active hours ({ACTIVE_START}:00–{ACTIVE_END}:00 MT). "
                f"Sleeping until {wake_at.strftime('%I:%M %p MT')} …"
            )
            await asyncio.sleep(wait)
            continue

        poll_count += 1

        # Create a fresh page for this poll cycle to prevent memory accumulation
        page = await context.new_page()
        log.info(f"[general] Poll #{poll_count} - created fresh page to manage memory")

        try:
            # Check if we should clear seen items (every 24 hours while running)
            if should_clear_seen():
                log.info(f"[general] ⏰ {CLEAR_INTERVAL_HOURS}h passed — clearing seen items to catch re-listed products")
                mark_cleared()
                seen.clear()
                save_seen(seen)

            for url in TARGET_URLS:
                log.info(f"[general] Checking {url} …")
                products = await scrape_all_products(page, url)
                log.info(f"[general]  Total unique products harvested: {len(products)}")

                for product in products:
                    pid   = product.get("id") or product.get("url")
                    title = product.get("title", "")

                    if not pid or pid in seen:
                        continue

                    kw_hit = keywords_match(title)
                    style_hit, found_style = style_number_match(product["url"])

                    if not kw_hit and not style_hit:
                        continue

                    match_reason = []
                    if kw_hit:
                        match_reason.append(f"keywords {KEYWORDS}")
                    if style_hit:
                        match_reason.append(f"style #{found_style}")

                    log.info(f"[general]  MATCH ({', '.join(match_reason)}): {title}  ({product.get('price', '?')})")
                    log.info(f"[general]  URL: {product.get('url')}")

                    await bag_and_notify(page, product, match_reason)

                    seen.add(pid)
                    save_seen(seen)

        finally:
            await page.close()
            log.info(f"[general] Poll #{poll_count} complete - closed page to free memory")

        delay = random.uniform(POLL_MIN, POLL_MAX)
        log.info(f"[general] Sleeping {delay:.0f}s …\n")
        await asyncio.sleep(delay)


# ── Grail loop (tight-interval rare-item watcher) ────────────────────────────

async def run_grail_loop(context: BrowserContext):
    _RARE.update(load_rare_items())
    if not _RARE.get("style_numbers") and not _RARE.get("url_patterns"):
        log.info("[grail] rare_items.json has no entries — grail loop disabled.")
        return

    tab_count = max(1, GRAIL_TABS)
    pages = [await context.new_page() for _ in range(tab_count)]

    log.info(
        f"[grail] Poll interval: {GRAIL_POLL_MIN}–{GRAIL_POLL_MAX}s  |  "
        f"Tabs: {tab_count}  |  Cooldown: {GRAIL_COOLDOWN_SECONDS}s (retry {GRAIL_RETRY_COOLDOWN_SECONDS}s)"
    )

    async def check_bucket(page: Page, urls: list[str]):
        for url in urls:
            products = await scrape_grail_page(page, url)
            for product in products:
                purl = product.get("url", "")
                if not purl or not is_rare_item(purl):
                    continue

                pid = product.get("id") or purl
                if grail_on_cooldown(pid):
                    continue

                title = product.get("title", "")
                log.info(f"[grail]  MATCH (rare item): {title}  ({product.get('price', '?')})")
                log.info(f"[grail]  URL: {purl}")

                bagged = await bag_and_notify(page, product, ["rare item (grail)"])
                grail_start_cooldown(
                    pid, GRAIL_COOLDOWN_SECONDS if bagged else GRAIL_RETRY_COOLDOWN_SECONDS
                )

    try:
        poll_count = 0
        while True:
            wait = seconds_until_active()
            if wait > 0:
                await asyncio.sleep(min(wait, GRAIL_POLL_MAX))
                continue

            poll_count += 1

            # Reload rare items on every poll so edits take effect without restart
            _RARE.update(load_rare_items())
            urls = grail_search_urls()

            if not urls:
                log.info("[grail] No rare style numbers configured — nothing to watch.")
            else:
                log.info(f"[grail] Poll #{poll_count} - checking {len(urls)} grail queries across {tab_count} tab(s)")

                buckets: list[list[str]] = [[] for _ in pages]
                for i, u in enumerate(urls):
                    buckets[i % tab_count].append(u)

                await asyncio.gather(*(check_bucket(p, b) for p, b in zip(pages, buckets)))

            delay = random.uniform(GRAIL_POLL_MIN, GRAIL_POLL_MAX)
            log.info(f"[grail] Sleeping {delay:.0f}s …\n")
            await asyncio.sleep(delay)
    finally:
        for p in pages:
            await p.close()


# ── Entry point ───────────────────────────────────────────────────────────────

async def run():
    global _RARE, _cart_page
    _RARE = load_rare_items()

    log.info("Bot started.")
    log.info(f"  Rare styles: {len(_RARE.get('style_numbers', []))} (grail loop)")
    log.info(f"  VNC mode:    {'enabled' if USE_VNC else 'disabled (local/headless)'}")

    if not KEYWORDS and not STYLE_NUMBERS and not _RARE.get("style_numbers") and not _RARE.get("url_patterns"):
        log.error(
            "No KEYWORDS, STYLE_NUMBERS, or rare_items.json entries configured — nothing to search for. Exiting."
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

        # General (broad, slow) and grail (narrow, tight-interval) loops share
        # this one browser/context and run concurrently — no second Chromium
        # process, just a second set of tabs.
        loops = [run_general_loop(context), run_grail_loop(context)]

        if SHOW_CART_TAB and USE_VNC:
            try:
                _cart_page = await context.new_page()
                await _cart_page.goto(CART_URL, wait_until="domcontentloaded", timeout=30_000)
                await _cart_page.bring_to_front()
                log.info(f"Cart tab pinned: {CART_URL}")
                loops.append(cart_viewer_loop(_cart_page))
            except Exception as e:
                log.warning(f"Could not open pinned cart tab: {e}")
                _cart_page = None

        await asyncio.gather(*loops)


if __name__ == "__main__":
    asyncio.run(run())