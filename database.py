"""
database.py - Collection scraping and incremental database updating for cards_db.json.
"""
import json
import os
import time
from config import DB_PATH, URL, PROTOCOL_RANGES

JS_TILES = r"""
() => [...document.querySelectorAll('button[data-inspect-art]')].map(b => {
  const tile = b.closest('li, article') || (b.parentElement && b.parentElement.parentElement) || b.parentElement;
  return {
    art: b.dataset.inspectArt,
    label: b.getAttribute('aria-label'),
    tile: ((tile && tile.innerText) || '').trim().replace(/\s+/g, ' ').slice(0, 200)
  };
})
"""

JS_DIALOG = r"""
(dlg) => {
  const items = [];
  dlg.querySelectorAll('[aria-label], [title]').forEach(el => {
    items.push({
      aria: el.getAttribute('aria-label') || '',
      title: el.getAttribute('title') || '',
      text: (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 120)
    });
  });
  return {text: dlg.innerText || '', items};
}
"""


def load_db():
    """Load existing cards_db.json file."""
    try:
        with open(DB_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_db(entries):
    """Save raw collection metadata to cards_db.json."""
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)


def scroll_collection(page):
    """Scroll down collection page until all cards are loaded."""
    stable, last = 0, -1
    for _ in range(60):
        page.mouse.wheel(0, 3000)
        time.sleep(0.5)
        c = page.locator("button[data-inspect-art]").count()
        if c == last:
            stable += 1
            if stable >= 3:
                break
        else:
            stable, last = 0, c
    page.evaluate("window.scrollTo(0, 0)")


def read_dialog(page):
    """Extract card attributes from inspection dialog."""
    dlg = page.locator('dialog[open], [role="dialog"]').first
    dlg.wait_for(state="visible", timeout=5000)
    time.sleep(0.4)
    return dlg.evaluate(JS_DIALOG)


def close_dialog(page):
    """Close inspection dialog."""
    page.keyboard.press("Escape")
    time.sleep(0.4)


def update_db(page, force=False):
    """
    Perform incremental update of card database.
    Only opens cards that are new or whose quantity changed.
    """
    first_run = not os.path.exists(DB_PATH)
    old = {e["art"]: e for e in load_db() if "art" in e}
    print("Updating card database...")
    page.goto(URL + "collection")
    time.sleep(5)
    scroll_collection(page)

    tiles = page.evaluate(JS_TILES)
    if not tiles:
        print("  No cards detected on collection page. Retaining existing database.")
        return False

    new = {}
    todo = []
    for i, t in enumerate(tiles):
        prev = old.get(t["art"])
        if force or prev is None or prev.get("tile") != t["tile"] or "front" not in prev:
            todo.append(i)
        else:
            new[t["art"]] = prev

    seen = {t["art"] for t in tiles}
    removed = [a for a in old if a not in seen and a != "060"]
    changed = first_run or bool(removed)

    if not todo:
        print(f"  Database up to date ({len(tiles)} cards).")
    else:
        print(f"  Opening {len(todo)} card(s) out of {len(tiles)}...")

    buttons = page.locator("button[data-inspect-art]")
    for n, i in enumerate(todo, 1):
        t = tiles[i]
        entry = {"art": t["art"], "label": t["label"], "tile": t["tile"]}
        try:
            buttons.nth(i).click(timeout=5000)
            entry["front"] = read_dialog(page)
        except Exception as e:
            print(f"  [{n}/{len(todo)}] Error on {t['label']}: {str(e).splitlines()[0]}")
            close_dialog(page)
            if t["art"] in old:
                new[t["art"]] = old[t["art"]]
            continue
        close_dialog(page)
        new[t["art"]] = entry
        if t["art"] != "060":
            changed = True
        print(f"  [{n}/{len(todo)}] Scanned: {t['label'].replace('Inspect ', '')}")
        if n % 10 == 0:
            save_db(list(new.values()))

    save_db(list(new.values()))
    print(f"  Database saved ({len(new)} cards total).")
    return changed