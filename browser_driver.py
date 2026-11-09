"""
browser_driver.py - Chrome process launcher and Playwright helper utilities.
"""
import os
import random
import re
import subprocess
import time
import urllib.request
from config import CDP, CHROME_PATHS, PROFILE_DIR, URL, PAUSE_SCALE, LOG_PATH, SHOW_TURNS


def tlog(*args):
    """Log turn details to log file and optionally to console."""
    msg = time.strftime("%H:%M:%S ") + " ".join(str(a) for a in args)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 3_000_000:
            os.replace(LOG_PATH, LOG_PATH.replace(".txt", "_old.txt"))
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass
    if SHOW_TURNS:
        print(*args)


def cdp_ready():
    """Check if Chrome remote debugging port is responsive."""
    try:
        urllib.request.urlopen(CDP + "/json/version", timeout=1)
        return True
    except Exception:
        return False


def ensure_chrome():
    """Ensure Chrome is running with remote debugging enabled."""
    if cdp_ready():
        print("Chrome port 9222 already available. Reusing session.")
        return
    exe = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    if not exe:
        raise SystemExit("Error: chrome.exe not found. Please adjust CHROME_PATHS.")
    print("Launching Chrome browser...")
    subprocess.Popen([exe, "--remote-debugging-port=9222", f"--user-data-dir={PROFILE_DIR}", URL])
    for _ in range(30):
        if cdp_ready():
            time.sleep(4)
            return
        time.sleep(1)
    raise SystemExit("Error: Chrome failed to expose port 9222.")


def btn(page, pattern):
    """Locate first button or anchor matching text regex."""
    return page.locator("button, a").filter(has_text=re.compile(pattern, re.I)).first


def clickable(loc):
    """Return True if element is visible and enabled."""
    try:
        return loc.is_visible() and loc.is_enabled()
    except Exception:
        return False


def safe_click(loc, timeout=4000):
    """Attempt safe element click with error catching."""
    try:
        loc.click(timeout=timeout)
        return True
    except Exception as e:
        tlog("  Click failed:", str(e).splitlines()[0])
        return False


def label(loc):
    """Extract aria-label attribute safely."""
    try:
        return loc.get_attribute("aria-label", timeout=1000) or ""
    except Exception:
        return ""


def attr(loc, name):
    """Extract custom attribute safely."""
    try:
        return loc.get_attribute(name, timeout=1000) or ""
    except Exception:
        return ""


def latest_page(ctx):
    """Get active non-closed page from context or open new one."""
    pages = [pg for pg in ctx.pages if not pg.is_closed()]
    return pages[-1] if pages else ctx.new_page()


def pause(a=0.8, b=2.0):
    """Randomized delay scaled by PAUSE_SCALE."""
    time.sleep(random.uniform(a, b) * PAUSE_SCALE)


def snippet(page, n=400):
    """Return normalized body text excerpt."""
    try:
        return " ".join(page.inner_text("body").split())[:n]
    except Exception:
        return "?"


def close_overlays(page):
    """Close card inspection dialog overlays."""
    try:
        if page.locator("dialog.game-dialog").first.is_visible():
            page.keyboard.press("Escape")
            pause(0.3, 0.6)
    except Exception:
        pass


def close_hand_sheet(page):
    """Close open hand sheet dialogs."""
    try:
        x = page.locator(".ah-sheet button[aria-label*='lose' i]").first
        if x.is_visible():
            x.click(timeout=2000)
            return
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass