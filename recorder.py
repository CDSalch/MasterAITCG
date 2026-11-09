"""
Enregistreur : tu joues à la main dans le Chrome du bot, et ce script affiche
(et sauvegarde dans recorder_log.txt) :
  - sur quoi tu cliques / glisses (click, dragstart, drop, change),
  - les "snapshots" : la liste des boutons visibles dans les panneaux ouverts
    (combat, main, dialogues) et des boutons de jeu (guard, support, attach, resolve...),
    à chaque fois que cette liste change. On voit ainsi ce que l'interface propose,
    même pour les options sur lesquelles tu ne cliques pas.

Utilisation :
  1. Arrête main.py (Ctrl+C) s'il tourne.
  2. python recorder.py
  3. Joue à la main dans la fenêtre Chrome, puis Ctrl+C ici et envoie-moi le log.
Le script est passif : il ne clique jamais.
"""
import json

from playwright.sync_api import sync_playwright

from browser_driver import ensure_chrome, latest_page
from config import CDP

JS = r"""
(() => {
  if (window.__recInstalled) return;
  window.__recInstalled = true;
  const short = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    const cls = (typeof el.className === 'string' ? el.className : '')
      .split(/\s+/).filter(Boolean).slice(0, 3);
    if (cls.length) s += '.' + cls.join('.');
    for (const a of el.attributes) {
      if (a.name.startsWith('data-') || ['role', 'aria-label', 'title', 'draggable'].includes(a.name)) {
        s += '[' + a.name + '=' + a.value.slice(0, 40) + ']';
      }
    }
    return s;
  };
  const report = (type, e) => {
    const el = e.target;
    if (!el || !el.tagName) return;
    const chain = [];
    let cur = el;
    for (let i = 0; i < 5 && cur && cur.tagName; i++) {
      chain.push(short(cur));
      cur = cur.parentElement;
    }
    const text = (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 50);
    const value = el.tagName === 'SELECT' ? el.value : '';
    window.__recLog(JSON.stringify({
      type, x: Math.round(e.clientX || 0), y: Math.round(e.clientY || 0), text, value, chain
    }));
  };
  for (const t of ['click', 'dragstart', 'drop', 'change']) {
    document.addEventListener(t, (e) => report(t, e), true);
  }

  // ---- snapshots : ce que l'interface propose à l'écran -------------------
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  };
  const KEY = /resolve|guard|support|attach|defend|intercept|skip|pass|confirm|cancel|end turn/i;
  const snapshot = () => {
    const seen = new Set();
    const items = [];
    const add = (el, where) => {
      if (seen.has(el) || !vis(el)) return;
      seen.add(el);
      const text = (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 60);
      items.push(where + ' > ' + short(el) + (text ? ' "' + text + '"' : ''));
    };
    document.querySelectorAll('.bt-combat, .ah-sheet, dialog[open], [role=dialog]').forEach((p) => {
      if (!vis(p)) return;
      const where = short(p).split('[')[0];
      p.querySelectorAll('button, [draggable=true], [role=button], input, select').forEach((el) => add(el, where));
    });
    document.querySelectorAll('button, a, [role=button]').forEach((el) => {
      if (KEY.test(el.innerText || el.getAttribute('aria-label') || '')) add(el, 'page');
    });
    return items;
  };
  let last = '';
  setInterval(() => {
    const items = snapshot();
    const sig = items.join('|');
    if (sig !== last) {
      last = sig;
      window.__recLog(JSON.stringify({ type: 'snapshot', items: items.slice(0, 40) }));
    }
  }, 700);
})();
"""

out = open("recorder_log.txt", "a", encoding="utf-8")


def emit(line):
    print(line)
    out.write(line + "\n")
    out.flush()


def log(raw):
    d = json.loads(raw)
    if d["type"] == "snapshot":
        emit("snapshot --------------------")
        for it in d["items"]:
            emit("    " + it)
        return
    extra = f' value="{d["value"]}"' if d.get("value") else ""
    emit(f'{d["type"]:9} ({d["x"]},{d["y"]}) "{d["text"]}"{extra} | '
         + "  <  ".join(d["chain"]))


def attach(pg):
    try:
        pg.expose_function("__recLog", log)
        pg.add_init_script(JS)
        pg.evaluate(JS)
    except Exception as e:
        print("Attache impossible sur une page :", str(e).splitlines()[0])


def main():
    ensure_chrome()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        for pg in ctx.pages:
            attach(pg)
        ctx.on("page", attach)
        print("Enregistrement en cours. Joue à la main dans Chrome. Ctrl+C pour arrêter.\n")
        try:
            while True:
                latest_page(ctx).wait_for_timeout(500)
        except KeyboardInterrupt:
            print("\nTerminé. Log sauvegardé dans recorder_log.txt")


if __name__ == "__main__":
    main()