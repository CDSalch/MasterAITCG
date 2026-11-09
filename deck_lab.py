"""
deck_lab.py - Construction automatique du deck dans le Deck Lab et sélection du « Battle deck ».

Port fidèle des fonctions de main_1.py (sélecteurs relevés avec recorder.py) :
  - build_deck_in_lab : crée un deck « Auto MMJJ-HHMM » avec la liste recommandée,
  - select_battle_deck : le choisit dans la liste « Battle deck » de l'Arena,
  - maybe_apply_deck : ne construit que si le deck recommandé diffère du dernier deck appliqué.

Corrections :
  - les impressions d'une même carte (ex. #010 et #010-P Pieter Levels) sont regroupées par nom :
    un seul passage de clics et un seul contrôle (plus de faux « 2 exemplaires au lieu de 1 »),
  - le Leader est choisi par la valeur de l'option du <select> (numéro de carte : 003 = Jensen Huang),
    ce qui évite les ambiguïtés avec les variantes « Full Art ALT »,
  - la vérification de sauvegarde cherche le deck dans le Deck Lab (article.lib-deck),
    puis dans la liste « Battle deck » du lobby de l'Arena (elle n'existe pas sur l'accueil).
"""
import json
import re
import sys
import time

import config
import game_engine
from browser_driver import clickable
from database import load_db
from deck_builder import load_state, save_state, parse_cards, optimize_and_save

BUILD_DECK = True            # --no-build pour désactiver
PAUSE_ON_NEW_DECK = True     # attendre Entrée si le deck ne peut pas être construit automatiquement
ACTIVE_DECK_NAME = None      # deck à sélectionner dans « Battle deck » (rempli par maybe_apply_deck)


def sheet_text(page):
    try:
        return " ".join(page.locator("aside.lib-deck-sheet").first.inner_text(timeout=2000).split())
    except Exception:
        return ""


def dump_deck_debug(page):
    """Écrit deck_debug.txt + deck_debug.png pour comprendre un écran du Deck Lab."""
    try:
        info = {
            "sheet": sheet_text(page)[:2000],
            "selects": page.locator("div.lib-deck-editor select").evaluate_all(
                "els => els.map(e => [...e.options].map(o => o.text))"),
            "buttons": page.locator("div.lib-deck-editor button").evaluate_all(
                "els => els.filter(e => !e.closest('.lib-pool-row'))"
                ".map(e => (e.getAttribute('aria-label') || e.innerText || '').trim())"
                ".filter(Boolean).slice(0, 80)"),
        }
        with open("deck_debug.txt", "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=1)
        page.screenshot(path="deck_debug.png")
        print("  Diagnostic écrit : deck_debug.txt et deck_debug.png")
    except Exception as e:
        print("  Diagnostic impossible :", str(e).splitlines()[0])


def choose_leader(page, leader_name, leader_art=None):
    """Sélectionne le Leader dans l'éditeur de deck. Retourne True si c'est fait.
    Priorité : valeur de l'option (numéro de carte), puis texte de l'option, puis bouton."""
    want = leader_name.lower()
    editor = page.locator("div.lib-deck-editor")
    try:
        # 0) <select class="lib-input"> : option dont la valeur est le numéro de carte (ex. "003")
        if leader_art:
            sels = editor.locator("select.lib-input")
            for i in range(sels.count()):
                s = sels.nth(i)
                values = s.locator("option").evaluate_all("els => els.map(o => o.value)")
                if leader_art in values:
                    s.select_option(value=leader_art)
                    time.sleep(0.8)
                    return True
        # 1) liste déroulante, par texte
        sels = editor.locator("select")
        for i in range(sels.count()):
            s = sels.nth(i)
            opts = [o.strip() for o in s.locator("option").all_inner_texts()]
            for idx, o in enumerate(opts):
                if want in o.lower():
                    s.select_option(index=idx)
                    time.sleep(0.8)
                    return True
        # 2) bouton / carte cliquable portant le nom du Leader (hors lignes de la liste de cartes)
        cands = [
            editor.locator("button, [role='radio'], [role='option'], [role='tab']")
            .filter(has_text=re.compile(re.escape(leader_name), re.I)),
            editor.locator(f'button[aria-label*="{leader_name}" i]'),
        ]
        for loc in cands:
            for i in range(loc.count()):
                el = loc.nth(i)
                if el.locator("xpath=ancestor::article[contains(@class,'lib-pool-row')]").count():
                    continue
                el.click(timeout=3000)
                time.sleep(0.8)
                return True
    except Exception as e:
        print("  Sélection du Leader :", str(e).splitlines()[0])
    # 3) aucun contrôle trouvé : le Leader est peut-être déjà celui affiché dans la fiche
    return want in sheet_text(page).lower()


def deck_count_of(page, name):
    """Nombre d'exemplaires déjà dans le deck, lu sur la ligne de la carte (ex. '... ×1 0')."""
    row = page.locator("article.lib-pool-row").filter(
        has=page.locator(f'button[aria-label="Add {name}"]')).first
    try:
        txt = " ".join(row.inner_text(timeout=2000).split())
    except Exception:
        return None
    m = re.search(r"×\s*\d+\s+(\d+)\s*$", txt)
    return int(m.group(1)) if m else None


def counts_by_name(cards, counts):
    """Regroupe les impressions d'une même carte (010 + 010-P -> « Pieter Levels » x2).
    Retourne une liste triée de (nom, kind, ram, total)."""
    by_name = {}
    for art, n in counts.items():
        c = cards[art]
        e = by_name.setdefault(c["name"], [c["kind"], c["ram"], 0])
        e[2] += n
    return sorted(((nm, k, r, n) for nm, (k, r, n) in by_name.items()),
                  key=lambda x: (x[1], x[2], x[0]))


def build_deck_in_lab(page, cards, leader, counts):
    """Crée un nouveau deck « Auto MMJJ-HHMM » avec la liste recommandée. Retourne son nom, ou None."""
    total = sum(counts.values())
    if total != config.DECK_SIZE:
        print(f"Deck recommandé invalide ({total} cartes au lieu de {config.DECK_SIZE}) : abandon.")
        return None
    name = time.strftime("Auto %m%d-%H%M")
    print(f"Création du deck « {name} » dans le Deck Lab ({leader['name']}, {total} cartes)...")
    try:
        page.goto(config.URL)
        time.sleep(3)
        lab = page.locator("button.quick-decks").first
        if clickable(lab):
            lab.click()
        else:
            page.goto(config.URL + "#decks")
        new_btn = page.locator("button.lib-primary").filter(
            has_text=re.compile(r"^\s*new deck\s*$", re.I)).first
        new_btn.wait_for(state="visible", timeout=10000)
        new_btn.click()
        page.locator("aside.lib-deck-sheet").first.wait_for(state="visible", timeout=8000)
        time.sleep(1)

        # Leader
        if not choose_leader(page, leader["name"], leader["art"]):
            print(f"  Impossible de choisir le Leader {leader['name']} automatiquement.")
            dump_deck_debug(page)
            if sys.stdin.isatty():
                input(f"  Choisis le Leader {leader['name']} dans le Deck Lab, puis appuie sur Entrée... ")
            else:
                return None

        # Nom du deck
        inp = page.locator("aside.lib-deck-sheet input.lib-input").first
        inp.fill(name)
        inp.press("Tab")

        # Cartes (regroupées par nom : les impressions d'une même carte partagent le même bouton « Add »)
        problems = []
        for cname, _kind, _ram, n in counts_by_name(cards, counts):
            sel = f'button[aria-label="Add {cname}"]'
            if page.locator(sel).count() == 0:
                problems.append(f"{cname} : introuvable dans la liste")
                continue
            add = page.locator(sel).first
            for _ in range(n):
                if not clickable(add):
                    break
                add.click(timeout=4000)
                time.sleep(0.25)
            got = deck_count_of(page, cname)
            if got is not None and got != n:
                problems.append(f"{cname} : {got} exemplaire(s) au lieu de {n}")
            print(f"  {n}x {cname}" + ("" if got in (None, n) else f"   <-- {got}"))
        if problems:
            print("  Problèmes :")
            for p in problems:
                print("   -", p)

        # Sauvegarde (le bouton n'est actif qu'avec exactement 30 cartes)
        save = page.locator("button.lib-primary").filter(
            has_text=re.compile(r"save deck", re.I)).first
        if not clickable(save):
            print("  Bouton « Save deck » inactif : le deck n'a pas exactement 30 cartes valides.")
            print("  Texte de la fiche :", sheet_text(page)[:300])
            dump_deck_debug(page)
            return None
        save.click()
        time.sleep(3)
        try:                                  # trace de l'écran juste après la sauvegarde
            page.screenshot(path="deck_after_save.png")
            print("  Fiche après sauvegarde :", sheet_text(page)[:200])
        except Exception:
            pass
        # Vérification fiable : le deck est-il présent dans le Deck Lab ou dans la liste « Battle deck » ?
        # (le bouton « Save deck » peut rester actif après un enregistrement réussi)
        if deck_in_arena(page, name):
            print(f"  Deck « {name} » enregistré.")
            return name
        print("  La sauvegarde ne semble pas avoir abouti : deck introuvable dans le Deck Lab et dans l'Arena.")
        return None
    except Exception as e:
        print("  Construction du deck impossible :", str(e).splitlines()[0])
        dump_deck_debug(page)
        return None


BATTLE_SELECT = 'select[aria-label="Select battle deck"]'


def open_arena(page):
    """Va dans le lobby de l'Arena (c'est là que se trouve la liste « Battle deck »)."""
    page.goto(config.URL)
    time.sleep(3)
    candidates = [
        page.locator("button.quick-arena").first,
        page.locator('nav.main-nav a[aria-label*="arena" i]').first,
        page.locator("nav.main-nav a").filter(has_text=re.compile(r"arena", re.I)).first,
        page.locator('nav.main-nav a[href*="arena" i]').first,
    ]
    for loc in candidates:
        if clickable(loc):
            try:
                loc.click(timeout=4000)
            except Exception:
                continue
            try:
                page.locator(BATTLE_SELECT).first.wait_for(state="visible", timeout=8000)
            except Exception:
                pass
            return True
    return False


def deck_in_arena(page, name):
    """True si le deck est dans le Deck Lab (article.lib-deck) ou dans la liste « Battle deck » de l'Arena."""
    want = name.lower()
    try:
        # 1) Deck Lab : liste des decks enregistrés (rechargée pour relire l'état sauvegardé)
        page.goto(config.URL + "#decks")
        page.reload()
        deck = page.locator("article.lib-deck").filter(has_text=re.compile(re.escape(name), re.I))
        try:
            deck.first.wait_for(state="visible", timeout=8000)
            return True
        except Exception:
            pass
        # 2) Lobby de l'Arena : liste « Battle deck »
        open_arena(page)
        sel = page.locator(BATTLE_SELECT).first
        opts = [o.strip().lower() for o in sel.locator("option").all_inner_texts()]
        if any(want in o for o in opts):
            return True
    except Exception as e:
        print("  Vérification impossible :", str(e).splitlines()[0])
    return False


def select_battle_deck(page, deck_name):
    """Choisit le deck dans <select aria-label="Select battle deck"> (options du type
    « Auto 1108-1454 · 30/30 », valeur = identifiant du deck). Retourne True si trouvé."""
    want = deck_name.lower()
    try:
        sel = page.locator(BATTLE_SELECT).first
        sel.wait_for(state="visible", timeout=8000)
        opts = [o.strip() for o in sel.locator("option").all_inner_texts()]
        for idx, o in enumerate(opts):
            if want in o.lower():
                sel.select_option(index=idx)
                print(f"Battle deck : « {o} »")
                return True
    except Exception as e:
        print("Sélection du Battle deck impossible :", str(e).splitlines()[0])
    return False


def maybe_apply_deck(page, force=False):
    """Si le deck recommandé n'est pas encore appliqué : le construit dans le Deck Lab
    (sinon te demande de le faire à la main), puis le retient comme Battle deck."""
    global ACTIVE_DECK_NAME
    st = load_state()
    deck = st.get("deck")
    if not deck:
        return
    if st.get("applied") == deck and not force:
        ACTIVE_DECK_NAME = st.get("applied_name")
        return
    cards = parse_cards(load_db())
    leader = cards.get(st.get("leader", ""))
    missing = [a for a in deck if a not in cards] + ([] if leader else [st.get("leader", "?")])
    if missing:
        print(f"Deck recommandé obsolète (cartes absentes de la base : {missing}) : recalcul...")
        optimize_and_save(page)
        st = load_state()
        deck = st.get("deck")
        cards = parse_cards(load_db())
        leader = cards.get(st.get("leader", ""))
        if not deck or not leader or any(a not in cards for a in deck):
            print("Recalcul impossible : relance avec --force-update.")
            return
        game_engine.load_scores()

    new_name = build_deck_in_lab(page, cards, leader, deck) if BUILD_DECK else None
    if new_name:
        st["applied"] = deck
        st["applied_name"] = new_name
        save_state(st)
        ACTIVE_DECK_NAME = new_name
        return
    if PAUSE_ON_NEW_DECK and sys.stdin.isatty():
        input(f"\nMets ton deck à jour à la main avec {config.DECK_FILE}, puis appuie sur Entrée... ")
        st["applied"] = deck
        st["applied_name"] = None
        save_state(st)