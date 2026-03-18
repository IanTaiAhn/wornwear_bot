"""
Worn Wear Monitor Bot
Polls wornwear.patagonia.com for items matching your keywords and/or style
numbers, then optionally adds to cart when found.

Includes:
  - Automatic Patagonia account login with session caching
  - Scroll-based "load more" to fetch the full catalog (not just first 24)
  - Style number matching (checks individual product pages for style #)

Requirements:
    uv sync
    uv run playwright install chromium

.env keys:
    KEYWORDS=synchilla,fleece,medium   # ALL must match listing title (comma-separated)
    STYLE_NUMBERS=25523,19975          # ANY match triggers alert (comma-separated, optional)
    PATAGONIA_EMAIL=your@email.com
    PATAGONIA_PASSWORD=yourpassword
    POLL_MIN=30
    POLL_MAX=65
    AUTO_ADD_CART=false
    NOTIFY_URL=https://ntfy.sh/your-topic

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

EMAIL    = os.getenv("PATAGONIA_EMAIL", "")
PASSWORD = os.getenv("PATAGONIA_PASSWORD", "")

STATE_FILE   = "seen_items.json"
SESSION_FILE = "patagonia_session.json"

# Proactively re-check session every N poll cycles (~47 min at default settings)
SESSION_CHECK_INTERVAL = 60

# How long to wait after each scroll before checking if new products loaded
SCROLL_PAUSE_MS = 2500
# Give up scrolling after this many consecutive scrolls with no new products
SCROLL_MAX_STALE = 3

TARGET_URLS = [
    "https://wornwear.patagonia.com/collections/just-added",
]

# ── Auth selectors ────────────────────────────────────────────────────────────
LOGIN_URL   = "https://www.patagonia.com/account/login/"
ACCOUNT_URL = "https://www.patagonia.com/account/"

EMAIL_SELECTORS = [
    "input#email",
    "input[name='email']",
    "input[type='email']",
    "input[autocomplete='email']",
    "input[autocomplete='username']",
    "input[placeholder*='email' i]",
]
PASSWORD_SELECTORS = [
    "input#password",
    "input[name='password']",
    "input[type='password']",
    "input[autocomplete='current-password']",
    "input[placeholder*='password' i]",
]
SUBMIT_SELECTORS = [
    "button[type='submit']",
    "button:has-text('Sign In')",
    "button:has-text('Log In')",
    "button:has-text('Login')",
    "input[type='submit']",
    "[data-testid*='login']",
    "[data-testid*='signin']",
]
LOGGED_IN_SIGNALS = [
    "a[href*='logout']",
    "a[href*='sign-out']",
    "button:has-text('Sign Out')",
    "button:has-text('Log Out')",
    "[data-testid*='account']",
    ".account-nav",
    ".account-dashboard",
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


# ── Auth helpers ──────────────────────────────────────────────────────────────

async def _find_visible(page: Page, selectors: list[str], label: str):
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                return el
        except Exception:
            continue
    log.warning(f"Could not locate {label} — selector list may need updating")
    return None

async def _save_session(context: BrowserContext):
    cookies = await context.cookies()
    with open(SESSION_FILE, "w") as f:
        json.dump(cookies, f, indent=2)
    log.info(f"Session saved → {SESSION_FILE} ({len(cookies)} cookies)")

async def _load_session(context: BrowserContext) -> bool:
    if not os.path.exists(SESSION_FILE):
        return False
    with open(SESSION_FILE) as f:
        cookies = json.load(f)
    await context.add_cookies(cookies)
    log.info(f"Loaded {len(cookies)} cookies from {SESSION_FILE}")
    return True

async def _is_logged_in(page: Page) -> bool:
    try:
        await page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2500)
    except Exception as e:
        log.warning(f"Auth check navigation failed: {e}")
        return False

    url = page.url
    if "login" in url.lower():
        return False
    for sel in LOGGED_IN_SIGNALS:
        try:
            if await page.locator(sel).first.is_visible(timeout=1500):
                return True
        except Exception:
            continue
    if "/account" in url and "login" not in url.lower():
        return True
    return False

async def _login(page: Page, context: BrowserContext) -> bool:
    if not EMAIL or not PASSWORD:
        log.error(
            "PATAGONIA_EMAIL and/or PATAGONIA_PASSWORD not set in .env — "
            "bot will run without an authenticated session."
        )
        return False

    log.info(f"Logging in via {LOGIN_URL} …")
    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        log.error(f"Failed to load login page: {e}")
        return False

    await page.wait_for_timeout(3000)

    email_el = await _find_visible(page, EMAIL_SELECTORS, "email input")
    if not email_el:
        log.error("Email input not found on login page.")
        return False
    await email_el.click()
    await email_el.fill(EMAIL)
    await page.wait_for_timeout(300)

    pw_el = await _find_visible(page, PASSWORD_SELECTORS, "password input")
    if not pw_el:
        log.error("Password input not found on login page.")
        return False
    await pw_el.click()
    await pw_el.fill(PASSWORD)
    await page.wait_for_timeout(300)

    submit_el = await _find_visible(page, SUBMIT_SELECTORS, "submit button")
    if not submit_el:
        log.error("Submit button not found on login page.")
        return False

    await submit_el.click()
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    await page.wait_for_timeout(3000)

    for sel in ["[role='alert']", "[class*='error']", ".form-error"]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1000):
                msg = (await el.inner_text()).strip()[:160]
                log.error(f"Login page returned an error: {msg!r}")
                return False
        except Exception:
            continue

    if "login" in page.url.lower():
        log.error(
            "Still on login page after submit — wrong credentials or bot-detection. "
            "Check PATAGONIA_EMAIL / PATAGONIA_PASSWORD in .env."
        )
        return False

    log.info("✅ Login successful!")
    await _save_session(context)
    return True

async def ensure_authenticated(page: Page, context: BrowserContext) -> bool:
    if await _load_session(context):
        if await _is_logged_in(page):
            log.info("✅ Reusing existing session.")
            return True
        log.info("Saved session has expired — logging in again …")
    return await _login(page, context)


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
    log.info(f"  Login:        {'enabled' if EMAIL else 'disabled (no credentials)'}")
    log.info(f"  Seen items:   {len(seen)} from previous runs")

    if not KEYWORDS and not STYLE_NUMBERS:
        log.error(
            "No KEYWORDS or STYLE_NUMBERS set in .env — nothing to search for. Exiting."
        )
        return

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

        # ── Initial login ─────────────────────────────────────────────────────
        if EMAIL:
            auth_ok = await ensure_authenticated(page, context)
            if not auth_ok:
                log.warning(
                    "Initial login failed — continuing without authentication. "
                    "Some items may not be visible or purchasable."
                )
        else:
            log.info("No credentials configured — running unauthenticated.")

        poll_count = 0

        # ── Poll loop ─────────────────────────────────────────────────────────
        while True:
            poll_count += 1

            # Periodic session health-check for long-running VPS deployments
            if EMAIL and poll_count % SESSION_CHECK_INTERVAL == 0:
                log.info(f"[Cycle {poll_count}] Periodic session check …")
                if not await _is_logged_in(page):
                    log.info("Session expired mid-run — re-authenticating …")
                    await _login(page, context)

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

                    await notify(
                        title="Worn Wear Match Found!",
                        body=(
                            f"{title} — {product.get('price', '')}\n"
                            f"Matched: {', '.join(match_reason)}\n"
                            f"{product.get('url', '')}"
                        ),
                    )

                    if AUTO_ADD_CART and product.get("url"):
                        # If we already loaded the product page for style checking,
                        # we're already on it — add_to_cart will navigate there again
                        # which is fine (idempotent).
                        success = await add_to_cart(page, product["url"])
                        if success:
                            await notify(
                                title="Added to Cart!",
                                body=(
                                    f"{title} was added to your cart.\n"
                                    f"{product.get('url', '')}"
                                ),
                            )
                        else:
                            await notify(
                                title="Add to Cart Failed",
                                body=(
                                    f"Found {title} but couldn't add to cart "
                                    f"— check logs.\n{product.get('url', '')}"
                                ),
                            )

                    seen.add(pid)
                    save_seen(seen)

            delay = random.uniform(POLL_MIN, POLL_MAX)
            log.info(f"Sleeping {delay:.0f}s …\n")
            await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(run())