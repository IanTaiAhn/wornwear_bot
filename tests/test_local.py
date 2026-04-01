"""
Local test script for the Worn Wear bot

Tests the bot's core functionality locally before deploying to the droplet.
This script simulates finding a match and triggers the add-to-cart + notification flow.

Usage:
    # Test with visible browser (watch it work)
    python test_local.py

    # Test headless (faster, no browser window)
    python test_local.py --headless
"""

import asyncio
import logging
import os
import sys
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("test-local")

# Import from bot.py
from bot import (
    add_to_cart,
    notify,
    cart_expiry_warning,
    NOTIFY_URL,
    USE_VNC,
    DROPLET_IP,
)


async def test_add_to_cart_flow(headless: bool = False):
    """
    Test the full flow:
    1. Navigate to a real Worn Wear product
    2. Add it to cart
    3. Send notification with noVNC link (if configured)
    4. Wait to see the 25-minute warning (or cancel early)
    """
    # Use a real product URL for testing (won't actually buy, just add to cart)
    test_product = {
        "title": "Test Item - Men's Better Sweater Fleece Jacket",
        "price": "$89.00",
        "url": "https://wornwear.patagonia.com/products/mens-better-sweater-fleece-jacket_25528_sth",
        "id": "test-25528",
    }

    log.info("=" * 60)
    log.info("WORN WEAR BOT — LOCAL TEST")
    log.info("=" * 60)
    log.info(f"  Headless mode:  {headless}")
    log.info(f"  VNC enabled:    {USE_VNC}")
    log.info(f"  Notify URL:     {NOTIFY_URL or '(not set)'}")
    log.info(f"  Droplet IP:     {DROPLET_IP or '(not set)'}")
    log.info("=" * 60)

    async with async_playwright() as pw:
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        if USE_VNC:
            launch_args.append("--display=:99")

        browser = await pw.chromium.launch(
            headless=headless,
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

        # Step 1: Add to cart
        log.info(f"\n[1/3] Adding test item to cart: {test_product['url']}")
        success = await add_to_cart(page, test_product["url"])

        if not success:
            log.error("❌ Add to cart failed — check the logs above")
            await browser.close()
            return

        log.info("✅ Item added to cart successfully!")

        # Step 2: Send notification
        log.info("\n[2/3] Sending notification...")

        novnc_url = f"http://{DROPLET_IP}:6080/vnc.html?autoconnect=true" if DROPLET_IP and USE_VNC else ""

        notification_body = (
            f"{test_product['title']} — {test_product['price']}\n"
            f"Matched: TEST MODE\n"
            f"{test_product['url']}\n"
            f"\n✅ Item bagged! You have 30 minutes."
        )

        if novnc_url:
            notification_body += f"\n\n🖥️ Complete checkout here:\n{novnc_url}"

        await notify(
            title="🧪 TEST — Worn Wear Item Bagged!",
            body=notification_body,
        )

        log.info("✅ Notification sent!")
        if NOTIFY_URL:
            log.info(f"   Check your ntfy app: {NOTIFY_URL}")
        else:
            log.warning("   (No NOTIFY_URL set — notification was skipped)")

        # Step 3: Test the 25-minute warning (with a shorter delay for testing)
        log.info("\n[3/3] Testing cart expiry warning...")
        log.info("   (Normally fires after 25 min, but we'll use 10 seconds for testing)")

        # Schedule the warning with a 10-second delay instead of 25 minutes
        warning_task = asyncio.create_task(
            cart_expiry_warning(test_product["title"], test_product["url"], delay_seconds=10)
        )

        log.info("   Waiting 10 seconds for expiry warning to fire...")
        log.info("   (Press Ctrl+C to cancel early)\n")

        try:
            await asyncio.wait_for(warning_task, timeout=15)
            log.info("✅ Expiry warning sent!")
        except asyncio.TimeoutError:
            log.warning("⚠️ Warning didn't fire in time")

        log.info("\n" + "=" * 60)
        log.info("TEST COMPLETE")
        log.info("=" * 60)
        log.info("Next steps:")
        log.info("  1. Check your phone/ntfy for the two notifications")
        if novnc_url:
            log.info(f"  2. Try opening the noVNC link: {novnc_url}")
        else:
            log.info("  2. Set DROPLET_IP + USE_VNC=true in .env to test noVNC links")
        log.info("  3. Check cart at: https://wornwear.patagonia.com/cart")
        log.info("  4. Clear your cart before running real bot")
        log.info("=" * 60)

        # Keep browser open for 30 seconds so you can inspect
        if not headless:
            log.info("\nBrowser will close in 30 seconds (or press Ctrl+C)...")
            try:
                await asyncio.sleep(30)
            except KeyboardInterrupt:
                log.info("Cancelled by user")

        await browser.close()


if __name__ == "__main__":
    headless = "--headless" in sys.argv

    try:
        asyncio.run(test_add_to_cart_flow(headless=headless))
    except KeyboardInterrupt:
        log.info("\nTest interrupted by user")
    except Exception as e:
        log.error(f"Test failed with error: {e}", exc_info=True)
