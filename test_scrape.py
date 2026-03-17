"""Quick test to see what products are on the page"""
import asyncio
from playwright.async_api import async_playwright

async def test_scrape():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.goto("https://wornwear.patagonia.com/collections/mens-fleece",
                       wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3000)

        # First, let's see what's on the page
        print("Taking screenshot and analyzing page structure...\n")
        await page.screenshot(path="page_debug.png")

        # Try to find all links with /products/ in them
        products = await page.evaluate("""
            () => {
                // Find all product links
                const links = Array.from(document.querySelectorAll('a[href*="/products/"]'));

                return links.map(link => {
                    const parent = link.closest('div[class*="product"], div[class*="card"], li, article') || link.parentElement;
                    return {
                        title: link.innerText?.trim() || link.querySelector('h1, h2, h3, h4, p')?.innerText?.trim() || '',
                        url: link.href,
                        id: link.href.split('/').pop(),
                        html: parent.outerHTML.substring(0, 200)
                    };
                }).filter(p => p.title && p.url);
            }
        """)

        await browser.close()

        # Print first 10 products
        print(f"Found {len(products)} products\n")
        for i, p in enumerate(products[:10], 1):
            if p.get('title'):
                print(f"{i}. {p['title']}")
                print(f"   Price: {p.get('price', 'N/A')}")
                print(f"   URL: {p.get('url', 'N/A')[:80]}...")
                print()

if __name__ == "__main__":
    asyncio.run(test_scrape())
