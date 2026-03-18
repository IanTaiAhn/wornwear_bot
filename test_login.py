"""
test_login.py — Automated login to wornwear.patagonia.com
via the Patagonia account login page at /account/login/

Usage:
    # Normal run (reads credentials from .env):
    uv run python test_login.py

    # Watch the browser in real time (strongly recommended on first run):
    HEADLESS=false uv run python test_login.py

    # Probe mode — dumps all inputs/buttons found on the login page without
    # actually submitting, useful for debugging selector failures:
    PROBE=true HEADLESS=false uv run python test_login.py

Add to your .env:
    PATAGONIA_EMAIL=your@email.com
    PATAGONIA_PASSWORD=yourpassword
"""

import asyncio
import json
import logging
import os

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Page, async_playwright

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("test-login")

# ── Config ────────────────────────────────────────────────────────────────────
EMAIL    = "djqnsoij@daiofd.com"
PASSWORD = "PasswordPapapa"
HEADLESS     = os.getenv("HEADLESS", "true").lower() != "false"
PROBE        = os.getenv("PROBE", "false").lower() == "true"

LOGIN_URL    = "https://www.patagonia.com/account/login/"
ACCOUNT_URL  = "https://www.patagonia.com/account/"
SESSION_FILE = "patagonia_session.json"

# ── Selector candidates ───────────────────────────────────────────────────────
# Listed roughly most-specific → most-generic.
# The script tries each in order and uses the first visible match.
# Run with PROBE=true to see what the page actually contains, then
# tighten these if needed.
EMAIL_SELECTORS = [
    "input#email",
    "input[name='email']",
    "input[type='email']",
    "input[autocomplete='email']",
    "input[autocomplete='username']",
    "input[placeholder*='email' i]",
    "input[placeholder*='Email' i]",
]

PASSWORD_SELECTORS = [
    "input#password",
    "input[name='password']",
    "input[type='password']",
    "input[autocomplete='current-password']",
    "input[placeholder*='password' i]",
    "input[placeholder*='Password' i]",
]

SUBMIT_SELECTORS = [
    "button[type='submit']",
    "button:has-text('Sign In')",
    "button:has-text('Log In')",
    "button:has-text('Login')",
    "input[type='submit']",
    "[data-testid*='login']",
    "[data-testid*='signin']",
    "[data-testid*='submit']",
]

# Signals that confirm we're on an authenticated account page
LOGGED_IN_SIGNALS = [
    "a[href*='logout']",
    "a[href*='sign-out']",
    "button:has-text('Sign Out')",
    "button:has-text('Log Out')",
    "[data-testid*='account']",
    ".account-nav",
    ".account-dashboard",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def find_visible(page: Page, selectors: list[str], label: str):
    """Return the first locator from the list that is currently visible."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                log.info(f"  ✓ Found {label} via: {sel}")
                return el
        except Exception:
            continue
    log.warning(f"  ✗ Could not locate {label} with any known selector")
    return None


async def probe_page(page: Page):
    """
    Dump every input and button on the page so you can identify the right
    selectors. Run with PROBE=true HEADLESS=false to inspect interactively.
    """
    log.info("── PROBE MODE ── dumping all inputs and buttons ──────────────────")
    elements = await page.evaluate("""
        () => {
            const nodes = [
                ...document.querySelectorAll('input'),
                ...document.querySelectorAll('button'),
                ...document.querySelectorAll('[type="submit"]'),
            ];
            return nodes.map(el => ({
                tag:          el.tagName.toLowerCase(),
                type:         el.type || '',
                id:           el.id || '',
                name:         el.name || '',
                placeholder:  el.placeholder || '',
                autocomplete: el.getAttribute('autocomplete') || '',
                className:    el.className || '',
                ariaLabel:    el.getAttribute('aria-label') || '',
                testId:       el.getAttribute('data-testid') || '',
                text:         el.innerText?.trim().slice(0, 60) || '',
                visible:      el.offsetParent !== null,
            }));
        }
    """)
    for el in elements:
        log.info(f"  {el}")
    log.info("── END PROBE ─────────────────────────────────────────────────────")


# ── Session persistence ───────────────────────────────────────────────────────

async def save_session(context: BrowserContext):
    cookies = await context.cookies()
    with open(SESSION_FILE, "w") as f:
        json.dump(cookies, f, indent=2)
    log.info(f"Session saved → {SESSION_FILE} ({len(cookies)} cookies)")


async def load_session(context: BrowserContext) -> bool:
    if not os.path.exists(SESSION_FILE):
        return False
    with open(SESSION_FILE) as f:
        cookies = json.load(f)
    await context.add_cookies(cookies)
    log.info(f"Loaded {len(cookies)} cookies from {SESSION_FILE}")
    return True


# ── Auth checks ───────────────────────────────────────────────────────────────

async def is_logged_in(page: Page) -> bool:
    """Navigate to the account page and check for authenticated-state signals."""
    log.info(f"Checking auth state via {ACCOUNT_URL} …")
    await page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(2500)

    url = page.url
    log.info(f"  Landed on: {url}")

    # Redirected to login → definitely not authenticated
    if "login" in url.lower():
        log.info("  → Redirected to login page, not authenticated")
        return False

    # Look for logout links / account-nav elements
    for sel in LOGGED_IN_SIGNALS:
        try:
            if await page.locator(sel).first.is_visible(timeout=1500):
                log.info(f"  → Authenticated signal found: {sel}")
                return True
        except Exception:
            continue

    # If URL looks like the account dashboard with no login redirect, assume OK
    if "/account" in url and "login" not in url.lower():
        log.info("  → URL looks like authenticated account page")
        return True

    return False


# ── Login flow ────────────────────────────────────────────────────────────────

async def login(page: Page, context: BrowserContext) -> bool:
    """
    Full login flow against https://www.patagonia.com/account/login/
    Returns True on success and saves the session to disk.
    """
    if not EMAIL or not PASSWORD:
        log.error(
            "PATAGONIA_EMAIL and PATAGONIA_PASSWORD must be set in .env. "
            "Copy .env.example → .env and fill them in."
        )
        return False

    log.info(f"Navigating to {LOGIN_URL} …")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(3000)  # let the React app hydrate

    log.info(f"  Page title : {await page.title()}")
    log.info(f"  Current URL: {page.url}")

    # Optional probe dump
    if PROBE:
        await probe_page(page)

    # ── Find and fill email ───────────────────────────────────────────────────
    email_el = await find_visible(page, EMAIL_SELECTORS, "email input")
    if not email_el:
        log.error(
            "Email input not found. Try:\n"
            "  PROBE=true HEADLESS=false uv run python test_login.py\n"
            "to inspect what's on the page, then add the correct selector to EMAIL_SELECTORS."
        )
        return False

    await email_el.click()
    await email_el.fill(EMAIL)
    await page.wait_for_timeout(400)

    # ── Find and fill password ────────────────────────────────────────────────
    pw_el = await find_visible(page, PASSWORD_SELECTORS, "password input")
    if not pw_el:
        log.error("Password input not found — see EMAIL_SELECTORS note above.")
        return False

    await pw_el.click()
    await pw_el.fill(PASSWORD)
    await page.wait_for_timeout(400)

    # ── Submit ────────────────────────────────────────────────────────────────
    submit_el = await find_visible(page, SUBMIT_SELECTORS, "submit button")
    if not submit_el:
        log.error("Submit button not found — see EMAIL_SELECTORS note above.")
        return False

    log.info("Submitting …")
    await submit_el.click()

    # Wait for navigation / React state change
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    await page.wait_for_timeout(3000)

    post_url = page.url
    log.info(f"Post-submit URL: {post_url}")

    # ── Check for inline error messages ──────────────────────────────────────
    error_selectors = [
        "[class*='error']:not([style*='display: none'])",
        "[role='alert']",
        ".form-error",
        "[data-testid*='error']",
    ]
    for sel in error_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1000):
                msg = (await el.inner_text()).strip()[:160]
                log.error(f"Login error message on page: {msg!r}")
                return False
        except Exception:
            continue

    # ── Confirm success ───────────────────────────────────────────────────────
    if "login" in post_url.lower():
        log.error(
            "Still on login page after submit. Possible causes:\n"
            "  • Wrong credentials (check PATAGONIA_EMAIL / PATAGONIA_PASSWORD)\n"
            "  • Patagonia added a CAPTCHA or 2-FA step\n"
            "  • Submit button was found but not the active one — run with PROBE=true"
        )
        return False

    log.info("✅ Login successful!")
    await save_session(context)
    return True


# ── Public entry point used by bot.py ─────────────────────────────────────────

async def ensure_authenticated(page: Page, context: BrowserContext) -> bool:
    """
    Call this at the start of each bot run:
      1. Try loading a saved session from disk.
      2. If still valid, skip the full login.
      3. Otherwise, run the full login flow.

    Returns True if we're authenticated, False if login failed.
    """
    if await load_session(context):
        log.info("Checking if saved session is still valid …")
        if await is_logged_in(page):
            log.info("✅ Reusing existing session — no login needed.")
            return True
        log.info("Session expired, logging in fresh …")

    return await login(page, context)


# ── Standalone test runner ────────────────────────────────────────────────────

async def run():
    if not EMAIL or not PASSWORD:
        print(
            "\n⚠️  Missing credentials.\n"
            "Add these to your .env file:\n"
            "  PATAGONIA_EMAIL=your@email.com\n"
            "  PATAGONIA_PASSWORD=yourpassword\n"
        )
        return

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

        success = await ensure_authenticated(page, context)

        if success:
            log.info(
                "✅ Authentication test PASSED.\n"
                f"   Session cookies saved to '{SESSION_FILE}'.\n"
                "   Import ensure_authenticated into bot.py to reuse this."
            )
        else:
            log.error(
                "❌ Authentication test FAILED.\n"
                "   Re-run with: PROBE=true HEADLESS=false uv run python test_login.py"
            )

        if not HEADLESS:
            log.info("Leaving browser open for 15 s so you can inspect the result …")
            await asyncio.sleep(15)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(run())