"""
test_add_to_cart.py — Tests the full add-to-cart flow on a single product.

Usage:
    # Test with the first matching product found automatically:
    python test_add_to_cart.py

    # Test with a specific product URL (skips search step):
    python test_add_to_cart.py --url "https://wornwear.patagonia.com/products/some-item"

    # Run headless=False to watch the browser in real time (recommended first time):
    HEADLESS=false python test_add_to_cart.py
"""

import asyncio
import argparse
import os
import logging
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("test-cart")

KEYWORDS   = os.getenv("KEYWORDS", "hoody").split(",")
HEADLESS   = os.getenv("HEADLESS", "true").lower() != "false"

LISTING_URL = "https://wornwear.patagonia.com/collections/mens-fleece"

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


def matches_keywords(text: str, keywords: list[str]) -> bool:
    text = text.lower()
    return all(kw.strip().lower() in text for kw in keywords)


async def find_first_match(page: Page) -> dict | None:
    """Scan the listing page and return the first keyword-matching product."""
    log.info(f"Scanning {LISTING_URL} for keywords: {KEYWORDS}")
    await page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(3000)

    products = await page.evaluate("""
        () => {
            const links = Array.from(document.querySelectorAll('a[href*="/products/"]'));
            return links.map(link => {
                const parent = link.closest(
                    'div[class*="product"], div[class*="card"], li, article'
                ) || link.parentElement;
                const priceEl = parent?.querySelector(
                    '[class*="price"], .price, [class*="Price"]'
                );
                return {
                    title: link.innerText?.trim() || '',
                    price: priceEl?.innerText?.trim() || '',
                    url:   link.href,
                    id:    link.href.split('/').pop()
                };
            }).filter(p => p.title && p.url);
        }
    """)

    log.info(f"Found {len(products)} products on listing page")

    for p in products:
        if matches_keywords(p["title"], KEYWORDS):
            log.info(f"✅ Matched: {p['title']}  ({p.get('price', 'N/A')})")
            return p

    log.warning("No keyword match found. Falling back to first available product.")
    if products:
        log.info(f"Using: {products[0]['title']}")
        return products[0]

    return None


async def attempt_add_to_cart(page: Page, product_url: str) -> bool:
    """
    Navigate to a product page, select a size, then attempt add-to-cart.
    Returns True if a button was clicked successfully.
    """
    log.info(f"Loading product page: {product_url}")
    await page.goto(product_url, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(2500)

    title = await page.title()
    log.info(f"Page title: {title}")

    # ── Step 1: find and click an available size ──────────────────────────────
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
                # Use a short timeout so we fail fast and move to the next size
                await label.click(timeout=2000)
                await page.wait_for_timeout(800)

                # Confirm the radio input is now actually checked
                is_checked = await page.evaluate(
                    f"() => document.getElementById('{size['id']}').checked"
                )
                if is_checked:
                    log.info(f"👕 Selected size: '{size['value']}'")
                    size_clicked = True
                    break
                else:
                    log.info(f"  '{size['value']}' click did not register as checked, trying next")
            except Exception:
                log.info(f"  '{size['value']}' label not clickable (likely sold out), trying next")

    except Exception as e:
        log.warning(f"Size selection error: {e}")

    if not size_clicked:
        log.error("❌ Could not select any available size — all may be sold out.")

    # ── Step 2: attempt add-to-cart ───────────────────────────────────────────
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

    for sel in ADD_TO_CART_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500) and await btn.is_enabled(timeout=1500):
                log.info(f"🛒 Found add-to-cart button via selector: {sel}")
                await btn.click()
                await page.wait_for_timeout(2500)

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
                    log.info(f"🎉 Cart confirmation signals detected: {cart_signals}")
                else:
                    log.info("Button clicked — no cart confirmation element found (may still have worked).")

                return True
        except Exception:
            continue

    log.error("❌ No add-to-cart button matched any known selector.")
    log.error("   Open the page in Chrome DevTools, inspect the Add to Cart button,")
    log.error("   and add its selector to ADD_TO_CART_SELECTORS in this file.")
    return False

# Not working
# async def clear_cart(page: Page):
#     """Navigate to cart and remove all items."""
#     log.info("Clearing cart...")
#     await page.goto("https://wornwear.patagonia.com/cart", wait_until="domcontentloaded", timeout=30_000)
#     await page.wait_for_timeout(2000)

#     while True:
#         try:
#             # The close icon's parent is the clickable remove button
#             remove_btn = page.locator(".icon-close").first
#             if await remove_btn.is_visible(timeout=2000):
#                 await remove_btn.locator("..").click()  # click the parent element
#                 await page.wait_for_timeout(1500)
#                 log.info("Removed one item from cart")
#             else:
#                 break
#         except Exception:
#             break

#     # Confirm cart is empty
#     cart_items = await page.locator(".cart-item__name").count()
#     if cart_items == 0:
#         log.info("✅ Cart is empty")
#     else:
#         log.warning(f"⚠️  {cart_items} item(s) still in cart after clearing")

async def run(product_url: str | None):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
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

        # ── Step 1: resolve the product URL ──────────────────────────────────
        if product_url:
            log.info(f"Using provided URL: {product_url}")
        else:
            match = await find_first_match(page)
            if not match:
                log.error("Could not find any product to test with. Exiting.")
                await browser.close()
                return
            product_url = match["url"]

        # ── Step 2: attempt add-to-cart ───────────────────────────────────────
        success = await attempt_add_to_cart(page, product_url)

        if success:
            log.info("✅ Add-to-cart test PASSED")
        else:
            log.warning("⚠️  Add-to-cart test FAILED — see selector debug output above")

        if not HEADLESS:
            log.info("Browser left open for 10s so you can inspect the result …")
            await asyncio.sleep(10)
        
        # await clear_cart(page)
        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test add-to-cart on Worn Wear")
    parser.add_argument(
        "--url",
        default=None,
        help="Skip the search step and go straight to this product URL",
    )
    args = parser.parse_args()
    asyncio.run(run(args.url))
