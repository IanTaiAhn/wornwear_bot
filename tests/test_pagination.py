"""
test_pagination.py — Standalone test for improved Load More pagination

Scrapes wornwear.patagonia.com/collections/just-added by clicking Load More
and polling the DOM directly for new products — no network interception needed.

KEY FEATURE — Unique item tracking (--track-unique):
    The site caps the *visible* DOM at ~577 items. Once that ceiling is hit,
    each Load More click rotates NEW products IN while pushing older ones OUT.
    --track-unique detects this AND harvests every product at every click so
    nothing is missed. Testing confirmed 77+ products were lost per session
    with the old end-of-pagination-only read approach.

Usage:
    uv run python test_pagination.py
    uv run python test_pagination.py --visible
    uv run python test_pagination.py --track-unique            # detect rotation only
    uv run python test_pagination.py --full-harvest            # recommended: capture everything
    uv run python test_pagination.py --full-harvest --visible
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

# Consecutive clicks with zero new unique products before declaring catalog exhausted
UNIQUE_STALE_LIMIT   = 3

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

EXTRACT_URLS_JS = """
    () => Array.from(
        new Set(
            Array.from(document.querySelectorAll('a[href*="/products/"]'))
                .map(a => a.href)
        )
    )
"""

EXTRACT_PRODUCTS_JS = """
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
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _style_key(url: str) -> str:
    """
    Extract a stable product identity from a Worn Wear URL, stripping the
    color-code suffix so the same jacket in two colors counts as ONE unique
    product for deduplication purposes.

    e.g. /products/mens-better-sweater-jacket_25528_sth
      -> /products/mens-better-sweater-jacket_25528

    Falls back to the full URL if no style number pattern is found.
    """
    import re
    m = re.search(r'(/products/[^_]+_\d{4,6})_', url)
    return m.group(1) if m else url


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


# ── Core scrapers ─────────────────────────────────────────────────────────────

async def scrape_end_of_pagination(
    page: Page,
    url: str,
    poll_ms: int,
    timeout_ms: int,
    scroll_pause_ms: int,
    stale_limit: int,
    max_clicks: int,
) -> tuple[list[dict], dict]:
    """
    Original approach: paginate to the end, read the DOM once.
    Used for --track-unique mode to show what gets missed.
    Returns (products, stats).
    """
    log.info(f"Navigating to: {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2500)
    except Exception as e:
        log.error(f"Failed to load page: {e}")
        return [], {}

    cumulative_unique_urls: set[str] = set()

    last_count   = await page.evaluate(COUNT_PRODUCTS_JS)
    stale_count  = 0
    unique_stale = 0
    click_count  = 0
    click_log: list[dict] = []
    start_time = time.time()

    initial_urls = await page.evaluate(EXTRACT_URLS_JS)
    for u in initial_urls:
        cumulative_unique_urls.add(_style_key(u))
    log.info(f"  Initial DOM count: {last_count}  |  initial unique: {len(cumulative_unique_urls)}")

    for iteration in range(max_clicks):
        elapsed = time.time() - start_time
        unique_before = len(cumulative_unique_urls)

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

        if btn is None:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(scroll_pause_ms)
            current_count = await page.evaluate(COUNT_PRODUCTS_JS)
            current_urls = await page.evaluate(EXTRACT_URLS_JS)
            for u in current_urls:
                cumulative_unique_urls.add(_style_key(u))
            new_unique = len(cumulative_unique_urls) - unique_before

            if current_count > last_count:
                log.info(
                    f"  [{iteration+1}] Scroll: {last_count} -> {current_count}"
                    f"  |  cumul unique: {len(cumulative_unique_urls)} (+{new_unique})"
                    f"  |  {elapsed:.1f}s"
                )
                last_count   = current_count
                stale_count  = 0
            else:
                stale_count += 1
                unique_stale = unique_stale + 1 if new_unique == 0 else 0
                log.info(
                    f"  [{iteration+1}] Scroll stale ({stale_count}/{stale_limit})"
                    f"  DOM: {current_count}  cumul unique: {len(cumulative_unique_urls)}"
                    f"  |  {elapsed:.1f}s"
                )
                if stale_count >= stale_limit and unique_stale >= UNIQUE_STALE_LIMIT:
                    log.info("  Done.")
                    break
            continue

        await btn.scroll_into_view_if_needed()
        await page.wait_for_timeout(200)
        await btn.click()
        click_count += 1

        new_dom_count = await wait_for_more_products(page, last_count, poll_ms, timeout_ms)
        current_urls = await page.evaluate(EXTRACT_URLS_JS)
        for u in current_urls:
            cumulative_unique_urls.add(_style_key(u))
        new_unique = len(cumulative_unique_urls) - unique_before
        rotation_flag = new_dom_count <= last_count and new_unique > 0

        click_log.append({
            "click":             click_count,
            "dom_before":        last_count,
            "dom_after":         new_dom_count,
            "dom_delta":         new_dom_count - last_count,
            "cumulative_unique": len(cumulative_unique_urls),
            "new_unique":        new_unique,
            "rotation":          rotation_flag,
        })

        if new_dom_count > last_count:
            log.info(
                f"  [{iteration+1}] DOM: {last_count} -> {new_dom_count}"
                f"  |  cumul unique: {len(cumulative_unique_urls)} (+{new_unique})"
                f"  |  {elapsed:.1f}s"
            )
            last_count   = new_dom_count
            stale_count  = 0
            unique_stale = 0
        else:
            stale_count  += 1
            unique_stale  = unique_stale + 1 if new_unique == 0 else 0
            rot_note = f"  ⚠️  rotating +{new_unique} new" if rotation_flag else ""
            log.info(
                f"  [{iteration+1}] DOM stale ({stale_count}/2) -- {new_dom_count}{rot_note}"
                f"  |  cumul unique: {len(cumulative_unique_urls)}"
                f"  |  {elapsed:.1f}s"
            )

            if stale_count >= 2 and unique_stale >= UNIQUE_STALE_LIMIT:
                log.info("  Catalog fully exhausted.")
                break
            if stale_count >= 2:
                stale_count = 0  # DOM capped but unique still growing — keep going

    products = await page.evaluate(EXTRACT_PRODUCTS_JS)
    stats = {
        "click_log":         click_log,
        "cumulative_unique": len(cumulative_unique_urls),
        "dom_final":         last_count,
        "total_clicks":      click_count,
        "elapsed_s":         time.time() - start_time,
        "mode":              "track-unique",
    }
    return products, stats


async def scrape_full_harvest(
    page: Page,
    url: str,
    poll_ms: int,
    timeout_ms: int,
    scroll_pause_ms: int,
    max_clicks: int,
) -> tuple[list[dict], dict]:
    """
    Full-harvest approach: accumulate products into a persistent dict at EVERY
    click so nothing is lost when items rotate out of the DOM.

    This is the approach used in the fixed bot.py and will reveal the true
    total catalog size — expected to be 8000+ on just-added.
    """
    log.info(f"Navigating to: {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2500)
    except Exception as e:
        log.error(f"Failed to load page: {e}")
        return [], {}

    # url -> product dict — the real catalog, built incrementally
    all_products: dict[str, dict] = {}

    async def harvest() -> int:
        """Snapshot current DOM products into all_products. Returns newly added count."""
        products = await page.evaluate(EXTRACT_PRODUCTS_JS)
        new_count = 0
        for p in products:
            if p["url"] not in all_products:
                all_products[p["url"]] = p
                new_count += 1
        return new_count

    # Initial harvest before any clicking
    initial_new    = await harvest()
    last_dom_count = await page.evaluate(COUNT_PRODUCTS_JS)
    log.info(f"  Initial: {last_dom_count} in DOM  |  {len(all_products)} harvested")

    stale_count  = 0
    unique_stale = 0
    click_count  = 0
    click_log: list[dict] = []
    start_time = time.time()
    current_dom_count = last_dom_count  # ensure defined if loop never runs

    for iteration in range(max_clicks):
        elapsed = time.time() - start_time

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(scroll_pause_ms)

        # Try Load More button
        for sel in LOAD_MORE_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=800) and await btn.is_enabled(timeout=800):
                    await btn.scroll_into_view_if_needed()
                    await page.wait_for_timeout(200)
                    log.info(f"  Clicking '{sel}'...")
                    await btn.click()
                    break
            except Exception:
                continue

        # Wait for DOM to update after click/scroll
        current_dom_count = await wait_for_more_products(page, last_dom_count, poll_ms, timeout_ms)

        # Harvest BEFORE the next click — captures items before they rotate out
        new_unique     = await harvest()
        total_harvested = len(all_products)
        click_count    += 1

        dom_grew  = current_dom_count > last_dom_count
        rotation  = not dom_grew and new_unique > 0

        click_log.append({
            "click":           click_count,
            "dom_before":      last_dom_count,
            "dom_after":       current_dom_count,
            "dom_delta":       current_dom_count - last_dom_count,
            "total_harvested": total_harvested,
            "new_unique":      new_unique,
            "rotation":        rotation,
        })

        if dom_grew:
            log.info(
                f"  [{iteration+1}] DOM: {last_dom_count} -> {current_dom_count}"
                f" (+{current_dom_count - last_dom_count})"
                f"  |  harvested: {total_harvested} (+{new_unique})  |  {elapsed:.1f}s"
            )
            last_dom_count = current_dom_count
            stale_count    = 0
            unique_stale   = 0

        elif rotation:
            log.info(
                f"  [{iteration+1}] DOM capped at {current_dom_count}"
                f"  |  ⚠️  rotating: +{new_unique} new unique"
                f"  |  total harvested: {total_harvested}  |  {elapsed:.1f}s"
            )
            stale_count  = 0  # DOM is flat but catalog isn't exhausted — keep going
            unique_stale = 0

        else:
            stale_count  += 1
            unique_stale += 1
            log.info(
                f"  [{iteration+1}] No growth"
                f"  (stale {unique_stale}/{UNIQUE_STALE_LIMIT})"
                f"  |  total harvested: {total_harvested}  |  {elapsed:.1f}s"
            )
            if unique_stale >= UNIQUE_STALE_LIMIT:
                log.info("  Catalog fully exhausted — stopping.")
                break

    total_elapsed = time.time() - start_time
    log.info(
        f"  Full harvest complete: {len(all_products)} unique products"
        f"  |  final DOM: {current_dom_count}"
        f"  |  {click_count} clicks  |  {total_elapsed:.1f}s"
    )

    stats = {
        "click_log":       click_log,
        "total_harvested": len(all_products),
        "dom_final":       current_dom_count,
        "total_clicks":    click_count,
        "elapsed_s":       total_elapsed,
        "mode":            "full-harvest",
    }
    return list(all_products.values()), stats


# ── Summary printers ──────────────────────────────────────────────────────────

def print_track_unique_summary(stats: dict):
    click_log = stats.get("click_log", [])
    if not click_log:
        return

    log.info("")
    log.info("── UNIQUE ITEM TRACKING TABLE ────────────────────────────────────────")
    log.info(
        f"  {'Click':>5}  {'DOM before':>10}  {'DOM after':>9}  {'DOM +/-':>7}"
        f"  {'Cumul. unique':>13}  {'New unique':>10}  {'Rotating?':>9}"
    )
    log.info(f"  {'─'*5}  {'─'*10}  {'─'*9}  {'─'*7}  {'─'*13}  {'─'*10}  {'─'*9}")

    rotation_clicks = []
    for row in click_log:
        rotating = "⚠️  YES" if row["rotation"] else ""
        if row["rotation"]:
            rotation_clicks.append(row["click"])
        log.info(
            f"  {row['click']:>5}  "
            f"{row['dom_before']:>10}  "
            f"{row['dom_after']:>9}  "
            f"{row['dom_delta']:>+7}  "
            f"{str(row['cumulative_unique']):>13}  "
            f"{str(row['new_unique']):>10}  "
            f"{rotating}"
        )

    log.info(f"  {'─'*5}  {'─'*10}  {'─'*9}  {'─'*7}  {'─'*13}  {'─'*10}  {'─'*9}")

    if rotation_clicks:
        log.info(f"\n  ⚠️  Rotation detected at clicks: {rotation_clicks}")
        log.info("  Use --full-harvest to capture every product.")

    cum = stats.get("cumulative_unique", 0)
    dom = stats.get("dom_final", 0)
    if cum > dom:
        log.info(
            f"\n  📊 Final DOM: {dom}  |  Cumulative unique: {cum}"
            f"  |  Missed by end-only read: {cum - dom}"
        )
    log.info("── END TRACKING ──────────────────────────────────────────────────────")


def print_full_harvest_summary(stats: dict, products: list[dict]):
    click_log = stats.get("click_log", [])
    if not click_log:
        return

    rotation_clicks = [r["click"] for r in click_log if r["rotation"]]
    peak_dom        = max((r["dom_after"] for r in click_log), default=0)
    total_harvested = stats.get("total_harvested", len(products))

    log.info("")
    log.info("── FULL HARVEST TABLE ────────────────────────────────────────────────")
    log.info(
        f"  {'Click':>5}  {'DOM before':>10}  {'DOM after':>9}  {'DOM +/-':>7}"
        f"  {'Total harvested':>15}  {'New unique':>10}  {'Rotating?':>9}"
    )
    log.info(f"  {'─'*5}  {'─'*10}  {'─'*9}  {'─'*7}  {'─'*15}  {'─'*10}  {'─'*9}")

    for row in click_log:
        rotating = "⚠️  YES" if row["rotation"] else ""
        log.info(
            f"  {row['click']:>5}  "
            f"{row['dom_before']:>10}  "
            f"{row['dom_after']:>9}  "
            f"{row['dom_delta']:>+7}  "
            f"{row['total_harvested']:>15}  "
            f"{row['new_unique']:>10}  "
            f"{rotating}"
        )

    log.info(f"  {'─'*5}  {'─'*10}  {'─'*9}  {'─'*7}  {'─'*15}  {'─'*10}  {'─'*9}")
    log.info("")
    log.info(f"  Peak DOM visible at once:    {peak_dom}")
    log.info(f"  Total unique harvested:      {total_harvested}")
    log.info(f"  Would have missed (old bot): {max(0, total_harvested - peak_dom)}")
    if rotation_clicks:
        log.info(f"  Rotation started at click:   {min(rotation_clicks)}")
    log.info("── END HARVEST TABLE ─────────────────────────────────────────────────")


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(args):
    mode = "full-harvest" if args.full_harvest else ("track-unique" if args.track_unique else "standard")

    log.info("=" * 60)
    log.info("PAGINATION TEST")
    log.info("=" * 60)
    log.info(f"  URL:            {args.url}")
    log.info(f"  Mode:           {mode}")
    log.info(f"  Headless:       {not args.visible}")
    log.info(f"  Poll interval:  {args.poll}ms")
    log.info(f"  Click timeout:  {args.timeout}ms (per click)")
    log.info(f"  Scroll pause:   {args.scroll_pause}ms")
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

        if args.full_harvest:
            products, stats = await scrape_full_harvest(
                page,
                url=args.url,
                poll_ms=args.poll,
                timeout_ms=args.timeout,
                scroll_pause_ms=args.scroll_pause,
                max_clicks=args.max_clicks,
            )
        else:
            products, stats = await scrape_end_of_pagination(
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
        log.info(f"  Mode:                   {mode}")
        log.info(f"  Products returned:      {len(products)}")
        if args.full_harvest:
            log.info(f"  Total unique harvested: {stats.get('total_harvested', len(products))}")
            dom    = stats.get("dom_final", 0)
            missed = max(0, stats.get("total_harvested", 0) - dom)
            if missed:
                log.info(f"  Would have missed:      {missed} (with old end-only read)")
        elif args.track_unique:
            cum = stats.get("cumulative_unique", 0)
            log.info(f"  Cumulative unique seen: {cum}")
            log.info(f"  Missed by end-only read: {cum - len(products)}")
        log.info(f"  Time elapsed:           {elapsed:.1f}s")
        log.info(f"  Clicks:                 {stats.get('total_clicks', '?')}")
        log.info("")
        if products:
            log.info("  Sample (first 5):")
            for p in products[:5]:
                log.info(f"    - {p['title'][:65]:<65}  {p.get('price', '?')}")
            if len(products) > 5:
                log.info(f"    ... and {len(products) - 5} more")
        log.info("=" * 60)

        if args.full_harvest:
            print_full_harvest_summary(stats, products)
        elif args.track_unique:
            print_track_unique_summary(stats)

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
    parser.add_argument(
        "--track-unique", action="store_true",
        help=(
            "Track cumulative unique URLs across every click to detect rotation "
            "and quantify how many products an end-only read misses. "
            "Does NOT fix the problem — use --full-harvest for that."
        ),
    )
    parser.add_argument(
        "--full-harvest", action="store_true",
        help=(
            "Accumulate products into a persistent dict at EVERY click so nothing "
            "is lost when items rotate out of the DOM. This is the fixed approach "
            "used in bot.py and will capture the true total catalog size."
        ),
    )
    parser.add_argument("--poll", type=int, default=DEFAULT_POLL_MS,
                        help=f"DOM poll interval in ms (default: {DEFAULT_POLL_MS})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS,
                        help=f"Max ms to wait for new products after a click (default: {DEFAULT_TIMEOUT_MS})")
    parser.add_argument("--scroll-pause", type=int, default=DEFAULT_SCROLL_PAUSE,
                        help=f"ms to wait after scroll fallback (default: {DEFAULT_SCROLL_PAUSE})")
    parser.add_argument("--stale-limit", type=int, default=DEFAULT_STALE_LIMIT,
                        help=f"Scroll stales before stopping, standard mode only (default: {DEFAULT_STALE_LIMIT})")
    parser.add_argument("--max-clicks", type=int, default=DEFAULT_MAX_CLICKS,
                        help=f"Safety cap on total clicks (default: {DEFAULT_MAX_CLICKS})")

    args = parser.parse_args()

    if args.full_harvest and args.track_unique:
        parser.error("--full-harvest and --track-unique are mutually exclusive. Pick one.")

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log.info("Interrupted.")


# Useful runs:
#
# Standard (old behaviour, for comparison):
#   uv run python test_pagination.py
#
# Detect rotation, quantify missed products:
#   uv run python test_pagination.py --track-unique
#
# Full harvest — capture the real catalog size:
#   uv run python test_pagination.py --full-harvest
#
# Full harvest on mens collection:
#   uv run python test_pagination.py --full-harvest --url "https://wornwear.patagonia.com/collections/mens"
#
# Full harvest on womens:
#   uv run python test_pagination.py --full-harvest --url "https://wornwear.patagonia.com/collections/womens"


# You can run on other urls in this code now.# Test the mens collection to see if it has more items
# uv run python test_pagination.py --url "https://wornwear.patagonia.com/collections/mens"

# # And womens
# uv run python test_pagination.py --url "https://wornwear.patagonia.com/collections/womens"

# 2026-03-31 16:05:28  INFO     ============================================================
# 2026-03-31 16:05:28  INFO     RESULTS
# 2026-03-31 16:05:28  INFO     ============================================================
# 2026-03-31 16:05:28  INFO       Mode:                   full-harvest
# 2026-03-31 16:05:28  INFO       Products returned:      999
# 2026-03-31 16:05:28  INFO       Total unique harvested: 999
# 2026-03-31 16:05:28  INFO       Would have missed:      430 (with old end-only read)
# 2026-03-31 16:05:28  INFO       Time elapsed:           458.1s
# 2026-03-31 16:05:28  INFO       Clicks:                 44
# 2026-03-31 16:05:28  INFO
# 2026-03-31 16:05:28  INFO       Sample (first 5):
# 2026-03-31 16:05:28  INFO         - Women's Better Sweater® Jacket                                     $59 - $74
# $59 to $74
# 2026-03-31 16:05:28  INFO         - Men's Nano Puff® Jacket                                            $115 - $143
# $115 to $143
# 2026-03-31 16:05:28  INFO         - Women's Lightweight Synchilla® Snap-T® Pullover                    $40 - $50
# $40 to $50
# 2026-03-31 16:05:28  INFO         - Women's Baggies™ Shorts - 5"                                       $29 - $37
# $29 to $37
# 2026-03-31 16:05:28  INFO         - Men's Down Sweater                                                 $114 - $142
# $114 to $142
# 2026-03-31 16:05:28  INFO         ... and 994 more
# 2026-03-31 16:05:28  INFO     ============================================================
# 2026-03-31 16:05:28  INFO
# 2026-03-31 16:05:28  INFO     ── FULL HARVEST TABLE ────────────────────────────────────────────────
# 2026-03-31 16:05:28  INFO       Click  DOM before  DOM after  DOM +/-  Total harvested  New unique  Rotating?
# 2026-03-31 16:05:28  INFO       ─────  ──────────  ─────────  ───────  ───────────────  ──────────  ─────────
# 2026-03-31 16:05:28  INFO           1          24         49      +25               48          24
# 2026-03-31 16:05:28  INFO           2          49         73      +24               72          24
# 2026-03-31 16:05:28  INFO           3          73         97      +24               96          24
# 2026-03-31 16:05:28  INFO           4          97        121      +24              120          24
# 2026-03-31 16:05:28  INFO           5         121        145      +24              144          24
# 2026-03-31 16:05:28  INFO           6         145        169      +24              168          24
# 2026-03-31 16:05:28  INFO           7         169        193      +24              192          24
# 2026-03-31 16:05:28  INFO           8         193        217      +24              216          24
# 2026-03-31 16:05:28  INFO           9         217        241      +24              240          24
# 2026-03-31 16:05:28  INFO          10         241        265      +24              264          24
# 2026-03-31 16:05:28  INFO          11         265        289      +24              288          24
# 2026-03-31 16:05:28  INFO          12         289        313      +24              312          24
# 2026-03-31 16:05:28  INFO          13         313        337      +24              336          24
# 2026-03-31 16:05:28  INFO          14         337        361      +24              360          24
# 2026-03-31 16:05:28  INFO          15         361        385      +24              384          24
# 2026-03-31 16:05:28  INFO          16         385        409      +24              408          24
# 2026-03-31 16:05:28  INFO          17         409        433      +24              432          24
# 2026-03-31 16:05:28  INFO          18         433        457      +24              456          24
# 2026-03-31 16:05:28  INFO          19         457        481      +24              480          24
# 2026-03-31 16:05:28  INFO          20         481        505      +24              504          24
# 2026-03-31 16:05:28  INFO          21         505        529      +24              528          24
# 2026-03-31 16:05:28  INFO          22         529        553      +24              552          24
# 2026-03-31 16:05:28  INFO          23         553        577      +24              576          24
# 2026-03-31 16:05:28  INFO          24         577        577       +0              600          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          25         577        577       +0              624          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          26         577        577       +0              648          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          27         577        577       +0              672          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          28         577        577       +0              696          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          29         577        577       +0              720          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          30         577        577       +0              744          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          31         577        577       +0              768          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          32         577        577       +0              792          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          33         577        577       +0              816          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          34         577        577       +0              840          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          35         577        577       +0              864          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          36         577        577       +0              888          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          37         577        577       +0              911          23  ⚠️  YES
# 2026-03-31 16:05:28  INFO          38         577        577       +0              935          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          39         577        577       +0              959          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          40         577        577       +0              983          24  ⚠️  YES
# 2026-03-31 16:05:28  INFO          41         577        569       -8              999          16  ⚠️  YES
# 2026-03-31 16:05:28  INFO          42         577        569       -8              999           0
# 2026-03-31 16:05:28  INFO          43         577        569       -8              999           0
# 2026-03-31 16:05:28  INFO          44         577        569       -8              999           0
# 2026-03-31 16:05:28  INFO       ─────  ──────────  ─────────  ───────  ───────────────  ──────────  ─────────
# 2026-03-31 16:05:28  INFO
# 2026-03-31 16:05:28  INFO       Peak DOM visible at once:    577
# 2026-03-31 16:05:28  INFO       Total unique harvested:      999
# 2026-03-31 16:05:28  INFO       Would have missed (old bot): 422
# 2026-03-31 16:05:28  INFO       Rotation started at click:   24
# 2026-03-31 16:05:28  INFO     ── END HARVEST TABLE ─────────────────────────────────────────────────