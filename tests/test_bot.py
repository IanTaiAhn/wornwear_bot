"""Test bot for one cycle only"""
import asyncio
import os
import logging
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("test")

KEYWORDS = os.getenv("KEYWORDS", "retro-x").split(",")

def matches_keywords(text: str, keywords: list[str]) -> bool:
    text = text.lower()
    return all(kw.strip().lower() in text for kw in keywords)

async def test():
    log.info(f"Testing with keywords: {KEYWORDS}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        url = "https://wornwear.patagonia.com/collections/mens-fleece"
        log.info(f"Fetching {url}")

        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3000)

        products = await page.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a[href*="/products/"]'));

                return links.map(link => {
                    const parent = link.closest('div[class*="product"], div[class*="card"], li, article') || link.parentElement;
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

        log.info(f"Found {len(products)} total products")

        matches = []
        for product in products:
            if matches_keywords(product['title'], KEYWORDS):
                matches.append(product)
                log.info(f"✅ MATCH: {product['title']}")
                log.info(f"   Price: {product.get('price', 'N/A')}")
                log.info(f"   URL: {product['url']}")

        if not matches:
            log.info("❌ No matches found")
            log.info(f"Sample products on page:")
            for p in products[:5]:
                log.info(f"  - {p['title']}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
