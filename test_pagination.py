"""
test_pagination.py — Standalone test for improved Load More pagination

Scrapes wornwear.patagonia.com/collections/just-added by clicking Load More
and polling the DOM directly for new products — no network interception needed.

Key insight: after clicking Load More, we poll every 500ms to see if the product
count has increased. We give up on a click only if the count hasn't changed after
a generous timeout. This is reliable regardless of how the site fetches data.

Usage:
    uv run python test_pagination.py
    uv run python test_pagination.py --visible
    uv run python test_pagination.py --url "https://wornwear.patagonia.com/collections/mens-fleece"
    uv run python test_pagination.py --timeout 15000    # per-click DOM poll timeout
"""

import asyncio
import argparse
import logging
import time

from playwright.async_api import async_playwright, Page

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("test-pagination")

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_URL          = "https://wornwear.patagonia.com/collections/just-added"
DEFAULT_POLL_MS      = 500    # how often to check the DOM after a click
DEFAULT_TIMEOUT_MS   = 12000  # give up on a click if no new products after this long
DEFAULT_SCROLL_PAUSE = 2000   # fallback scroll wait (when no button present)
DEFAULT_STALE_LIMIT  = 3      # consecutive no-button stales before stopping
DEFAULT_MAX_CLICKS   = 500

LOAD_MORE_SELECTORS = [
    "button:has-text('Load More')",
    "button:has-text('Show More')",
    "button:has-text('View More')",
    "[class*='load-more']",
    "[class*='LoadMore']",
]

COUNT_PRODUCTS_JS = """
    () => new Set(
        Array.from(document.querySelectorAll('a[href*="/products/"]'))
            .map(a => a.href)
    ).size
"""


async def wait_for_more_products(page: Page, baseline: int, poll_ms: int, timeout_ms: int) -> int:
    """
    Poll the DOM every `poll_ms` until the product count exceeds `baseline`
    or `timeout_ms` elapses. Returns the new count (may equal baseline on timeout).
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        await page.wait_for_timeout(poll_ms)
        count = await page.evaluate(COUNT_PRODUCTS_JS)
        if count > baseline:
            return count
    return await page.evaluate(COUNT_PRODUCTS_JS)


async def scrape_all_products(
    page: Page,
    url: str,
    poll_ms: int,
    timeout_ms: int,
    scroll_pause_ms: int,
    stale_limit: int,
    max_clicks: int,
) -> list[dict]:

    log.info(f"Navigating to: {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2500)
    except Exception as e:
        log.error(f"Failed to load page: {e}")
        return []

    last_count  = await page.evaluate(COUNT_PRODUCTS_JS)
    stale_count = 0
    click_count = 0
    start_time  = time.time()

    log.info(f"  Initial product count: {last_count}")

    for iteration in range(max_clicks):
        elapsed = time.time() - start_time

        # ── Find the Load More button ─────────────────────────────────────────
        btn = None
        matched_sel = None
        for sel in LOAD_MORE_SELECTORS:
            try:
                candidate = page.locator(sel).first
                if await candidate.is_visible(timeout=800) and await candidate.is_enabled(timeout=800):
                    btn = candidate
                    matched_sel = sel
                    break
            except Exception:
                continue

        # ── No button: try scrolling ──────────────────────────────────────────
        if btn is None:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(scroll_pause_ms)
            current_count = await page.evaluate(COUNT_PRODUCTS_JS)

            if current_count > last_count:
                log.info(
                    f"  [{iteration+1}] Scroll: {last_count} -> {current_count} "
                    f"(+{current_count - last_count})  |  {elapsed:.1f}s"
                )
                last_count  = current_count
                stale_count = 0
            else:
                stale_count += 1
                log.info(
                    f"  [{iteration+1}] Scroll stale ({stale_count}/{stale_limit}) "
                    f"-- still {current_count} products  |  {elapsed:.1f}s"
                )
                if stale_count >= stale_limit:
                    log.info("  No button and no new products from scrolling. Done.")
                    break
            continue

        # ── Button found: click and poll DOM ──────────────────────────────────
        await btn.scroll_into_view_if_needed()
        await page.wait_for_timeout(200)
        await btn.click()
        click_count += 1

        log.info(
            f"  [{iteration+1}] Clicked '{matched_sel}' -- "
            f"polling DOM (up to {timeout_ms}ms)..."
        )

        new_count = await wait_for_more_products(page, last_count, poll_ms, timeout_ms)

        if new_count > last_count:
            log.info(
                f"  [{iteration+1}] {last_count} -> {new_count} "
                f"(+{new_count - last_count})  |  {elapsed:.1f}s elapsed"
            )
            last_count  = new_count
            stale_count = 0
        else:
            stale_count += 1
            log.info(
                f"  [{iteration+1}] Clicked but no new products after {timeout_ms}ms "
                f"(stale {stale_count}/2) -- still {new_count}  |  {elapsed:.1f}s"
            )
            if stale_count >= 2:
                log.info("  Button clicked twice with no new products. End of catalog.")
                break

    total_elapsed = time.time() - start_time
    log.info(
        f"  Pagination done: {last_count} products, "
        f"{click_count} clicks, {total_elapsed:.1f}s"
    )

    # ── Extract full product list ─────────────────────────────────────────────
    log.info("Extracting product details...")
    products = await page.evaluate("""
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
                    id:    link.href.split('/').pop(),
                };
            }).filter(p => p.title && p.url);
        }
    """)
    return products


async def run(args):
    log.info("=" * 60)
    log.info("PAGINATION TEST")
    log.info("=" * 60)
    log.info(f"  URL:            {args.url}")
    log.info(f"  Headless:       {not args.visible}")
    log.info(f"  Poll interval:  {args.poll}ms")
    log.info(f"  Click timeout:  {args.timeout}ms (per click)")
    log.info(f"  Scroll pause:   {args.scroll_pause}ms")
    log.info(f"  Stale limit:    {args.stale_limit}")
    log.info(f"  Max clicks:     {args.max_clicks}")
    log.info("=" * 60)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not args.visible,
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
        wall_start = time.time()

        products = await scrape_all_products(
            page,
            url=args.url,
            poll_ms=args.poll,
            timeout_ms=args.timeout,
            scroll_pause_ms=args.scroll_pause,
            stale_limit=args.stale_limit,
            max_clicks=args.max_clicks,
        )

        elapsed = time.time() - wall_start

        log.info("")
        log.info("=" * 60)
        log.info("RESULTS")
        log.info("=" * 60)
        log.info(f"  Total products scraped: {len(products)}")
        log.info(f"  Time elapsed:           {elapsed:.1f}s")
        log.info(f"  Avg per product:        {elapsed / max(len(products), 1) * 1000:.0f}ms")
        log.info("")
        if products:
            log.info("  Sample (first 5):")
            for p in products[:5]:
                log.info(f"    - {p['title'][:65]:<65}  {p.get('price', '?')}")
            if len(products) > 5:
                log.info(f"    ... and {len(products) - 5} more")
        log.info("=" * 60)

        if args.visible:
            log.info("Browser open for 15s (Ctrl+C to quit early)")
            try:
                await asyncio.sleep(15)
            except KeyboardInterrupt:
                pass

        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Test Load More DOM-polling pagination against Worn Wear"
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--visible", action="store_true",
                        help="Show the browser window")
    parser.add_argument("--poll", type=int, default=DEFAULT_POLL_MS,
                        help=f"DOM poll interval in ms (default: {DEFAULT_POLL_MS})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS,
                        help=f"Max ms to wait for new products after a click (default: {DEFAULT_TIMEOUT_MS})")
    parser.add_argument("--scroll-pause", type=int, default=DEFAULT_SCROLL_PAUSE,
                        help=f"ms to wait after scroll fallback (default: {DEFAULT_SCROLL_PAUSE})")
    parser.add_argument("--stale-limit", type=int, default=DEFAULT_STALE_LIMIT,
                        help=f"Scroll stales before stopping (default: {DEFAULT_STALE_LIMIT})")
    parser.add_argument("--max-clicks", type=int, default=DEFAULT_MAX_CLICKS,
                        help=f"Safety cap on total clicks (default: {DEFAULT_MAX_CLICKS})")

    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log.info("Interrupted.")


# You can run on other urls in this code now.# Test the mens collection to see if it has more items
# uv run python test_pagination.py --url "https://wornwear.patagonia.com/collections/mens"

# # And womens
# uv run python test_pagination.py --url "https://wornwear.patagonia.com/collections/womens"