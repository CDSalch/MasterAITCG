"""
newmain.py : tout-en-un pour AI Masters TCG.

Au lancement, dans cet ordre :
  1. MET À JOUR la base de cartes (cards_db.json) : lit ta collection et n'ouvre que les cartes
     nouvelles ou dont le nombre d'exemplaires a changé (la 1re fois : toutes, 2 à 4 minutes).
  2. OPTIMISE le deck SI la collection a changé : simule des milliers de tirages aléatoires,
     écrit deck_recommande.txt et card_scores.json, puis CONSTRUIT le deck recommandé dans le
     Deck Lab (nouveau deck « Auto MMJJ-HHMM », 30 cartes) et le choisit comme Battle deck.
     Si la construction automatique échoue, il te demande de le faire à la main (Entrée ensuite).
  3. LANCE la boucle de parties : Arena -> Practice -> déployer, attaquer, END TURN...

Stratégie de jeu, à chaque tour de ton côté :
  - DÉPLOYER les meilleures cartes de la main (ordre de card_scores.json), plus les Protocoles
    de pioche sans cible ;
  - ATTAQUER avec chaque combattant prêt (Personnages d'abord, Leader en dernier) :
    Leader adverse si ma puissance >= sa défense (+1 aux tours 3, 5, 7), sinon KO du Personnage
    adverse épuisé le plus fort que je bats, sinon pas d'attaque ;
  - END TURN.
  Réglages OFFENSIFS par défaut (ceux qui faisaient ~56 % de victoires). Les variantes défensives
  (--reserve, --guard-home N, --margin auto, --defense-weight W) sont à tester une par une avec
  --report avant de les adopter. Le détail de chaque partie est dans game_log.txt.
  Marge adaptative : on estime, par comptage sur le deck adverse supposé (miroir du mien), la
  probabilité qu'il bloque avec un soutien, et on ajoute de la RAM de marge quand ça vaut le coup.
  Chaque partie ajoute une ligne à stats.csv ; « --report » compare les réglages (avec confiance).

Le script lance lui-même Chrome (profil C:\\chrome-bot, déjà connecté à X) avec le port
de débogage 9222. Si Chrome est déjà ouvert avec ce port, il le réutilise.

Options :
  --games N          nombre de parties d'entraînement (5 par défaut)
  --no-update        saute la mise à jour de la base de cartes
  --force-update     rouvre toutes les cartes de la collection
  --force-optimize   relance l'optimisation du deck même sans nouvelle carte
  --no-pause         ne s'arrête pas si le deck est à faire à la main
  --no-build         ne construit pas le deck dans le Deck Lab
  --build-deck       reconstruit le deck recommandé même s'il est déjà appliqué
  --reserve          TEST : garde RAM + Protocole de soutien pour la défense (désactivé par défaut)
  --guard-home N     TEST : PV en dessous desquels les Guards n'attaquent plus (0 = désactivé)
  --margin N|auto    marge de puissance au-dessus de la défense du Leader (0 par défaut ; auto = croyance, à tester)
  --defense-weight W TEST : valeur d'un coup bloqué dans l'optimisation du deck (0 = deck offensif)
  --deck NOM         impose le deck de l'Arena (début du nom suffit) et ne construit rien
  --speed X          multiplicateur des pauses (0.6 = plus rapide, à tester)
  --report           affiche le bilan de stats.csv (par réglage) et quitte

Prérequis : pip install playwright   (pas besoin de "playwright install")
Arrêt : Ctrl+C dans le terminal.
"""
import argparse
import csv
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter

from playwright.sync_api import sync_playwright

# ---------- Configuration ----------
URL = "https://aimasterstcg.com/"
PROFILE_DIR = r"C:\chrome-bot"
CDP = "http://127.0.0.1:9222"

MAX_GAMES = 5              # nombre de parties avant arrêt (--games)
STUCK_TIMEOUT = 120        # secondes sans action possible avant arrêt
MAX_DEPLOY_PER_TURN = 4    # cartes posées au maximum par tour
PLAY_PROTOCOLS = False     # True : exécute aussi les Protocoles (risque : effets qui demandent une cible)
ATTACK_MARGIN = 0          # marge de puissance FIXE au-dessus de la défense du Leader (si MARGIN_AUTO = False)
MARGIN_AUTO = False        # marge adaptative d'après la croyance sur la main adverse (--margin auto pour tester)
MARGIN_RAM_COST = 0.12     # prix d'1 RAM de marge, exprimé en « probabilité de toucher » (voir choose_margin)
BELIEF_SAMPLES = 400       # mains adverses tirées pour estimer la probabilité de blocage
PAUSE_SCALE = 1.0          # --speed : multiplie toutes les pauses (0.6 = plus rapide, à tester)
INTERCEPT_CHUMP_HP = 2     # en dessous de ce nombre de PV, on sacrifie un Guard plutôt que de perdre 1 PV
USE_RESERVE = False        # garder de la RAM + un Protocole de soutien pour défendre (--reserve pour tester)
KEEP_SUPPORT = 1           # Protocoles de soutien gardés en main (on ne les joue pas pour piocher)
GUARD_HOME_HP = 0          # sous ce nombre de PV, les Guards restent prêts à intercepter (0 = désactivé ; --guard-home 3 pour tester)
STATS_PATH = "stats.csv"   # une ligne par partie (--report pour le bilan par réglage)
DEFENSE_WEIGHT = 0.0       # valeur d'un coup bloqué dans la simulation du deck (0 = deck purement offensif ; --defense-weight 6 pour tester)

DB_PATH = "cards_db.json"
SCORES_PATH = "card_scores.json"
DECK_FILE = "deck_recommande.txt"
DECK_STATE_PATH = "deck_state.json"
ITERS = 400                # essais d'amélioration du deck par couleur secondaire
SIMS = 600                 # parties simulées par évaluation
PAUSE_ON_NEW_DECK = True   # attendre Entrée si le deck ne peut pas être construit automatiquement
BUILD_DECK = True          # construire le deck recommandé dans le Deck Lab (--no-build pour désactiver)

SHOW_TURNS = False         # True : réaffiche le détail des tours (déploiements, attaques, défense)

# Protocoles que le bot sait jouer sans choisir de cible (pioche)
SAFE_PROTOCOLS = {"040", "116", "098", "118"}
PROTOCOL_RANGES = [(40, 59), (96, 125)]   # numéros de cartes qui sont des Protocoles
MAX_COPIES = 3
SECONDARY_MAX = 10
DECK_SIZE = 30
SIM_TURNS = 7

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

# Sélecteurs relevés avec recorder.py
SEL_MY_LEADER = 'button.cb-portrait[data-seat="0"][data-fighter="leader"]'
SEL_OPP_LEADER = 'button.cb-portrait[data-seat="1"][data-fighter="leader"]'
SEL_ATTACH = 'button[title="Give this fighter +1 Power for your turn"]'
SEL_HAND = 'button.ah-card[data-hand-owner="self"][data-playable="true"]'
SEL_COMMIT = "button.ah-commit"
SEL_MY_ATTACKERS = ('button[data-seat="0"][aria-label^="Attack with"], '
                    'button.cb-piece[data-seat="0"].cb-pickable')
SEL_TARGETS = 'button[aria-label^="Target"]'

IN_GAME_RE = re.compile(
    r"your hand|your leader|end turn|resolve defense|your turn|your defense", re.I
)
END_RE = re.compile(
    r"^\s*(continue|ok|claim|back to arena|return to arena|play again|keep this order|confirm|done)\s*$", re.I
)
LEADER_BONUS_TURNS = (3, 5, 7)  # le Leader du 2e joueur défend à +1 ces tours-là

CARD_SCORES = {}  # rempli par load_scores()
ACTIVE_DECK_NAME = None  # deck à sélectionner dans « Battle deck » (rempli après la construction)

# ---------- Résultats des parties ----------
STATS = {"games": 0, "wins": 0, "tokens": 0}
WIN_RE = re.compile(r"\b(victory|you win|you won|win)\b", re.I)
LOSS_RE = re.compile(r"\b(defeat|you lose|you lost|loss)\b", re.I)
TOKEN_RES = [
    re.compile(r"\+\s*(\d[\d.,]*)\s*(?:\$?\w+\s+)?tokens?", re.I),
    re.compile(r"tokens?\D{0,15}\+?\s*(\d[\d.,]*)", re.I),
]
REPORT_DEBUG_DONE = False


LOG_PATH = "game_log.txt"   # détail de toutes les parties (à m'envoyer en cas de problème)


def tlog(*args):
    """Détail des tours : toujours écrit dans game_log.txt, affiché seulement si SHOW_TURNS = True."""
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 3_000_000:
            os.replace(LOG_PATH, LOG_PATH.replace(".txt", "_ancien.txt"))
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + " ".join(str(a) for a in args) + "\n")
    except Exception:
        pass
    if SHOW_TURNS:
        print(*args)


def record_result(page):
    """Lit l'écran de fin de partie et affiche : victoires/parties et tokens gagnés."""
    global REPORT_DEBUG_DONE
    try:
        txt = " ".join(page.inner_text("body").split())
    except Exception:
        txt = ""
    if not REPORT_DEBUG_DONE:      # une seule fois : pour ajuster les regex si besoin
        REPORT_DEBUG_DONE = True
        try:
            with open("match_report_debug.txt", "w", encoding="utf-8") as f:
                f.write(txt)
        except Exception:
            pass

    STATS["games"] += 1
    has_win = bool(WIN_RE.search(txt))
    has_loss = bool(LOSS_RE.search(txt))
    if has_win and not has_loss:
        STATS["wins"] += 1

    gain = 0
    for rx in TOKEN_RES:
        m = rx.search(txt)
        if m:
            try:
                gain = int(float(m.group(1).replace(",", "")))
            except ValueError:
                gain = 0
            break
    STATS["tokens"] += gain

    print(f"Victoires : {STATS['wins']}/{STATS['games']} | tokens gagnés : +{STATS['tokens']}")
    return "win" if (has_win and not has_loss) else ("loss" if has_loss else "?")


def load_scores():
    """Charge card_scores.json (priorités de déploiement) dans CARD_SCORES."""
    global CARD_SCORES
    try:
        with open(SCORES_PATH, encoding="utf-8") as f:
            CARD_SCORES = json.load(f)
        print(f"{SCORES_PATH} chargé ({len(CARD_SCORES)} cartes)")
    except Exception:
        CARD_SCORES = {}
        print(f"{SCORES_PATH} absent : déploiement par coût décroissant.")


# =====================================================================
# OUTILS : Chrome, helpers, logique de jeu
# =====================================================================
# ---------- Chrome ----------
def cdp_ready():
    try:
        urllib.request.urlopen(CDP + "/json/version", timeout=1)
        return True
    except Exception:
        return False


def ensure_chrome():
    if cdp_ready():
        print("Chrome déjà ouvert avec le port 9222, réutilisation.")
        return
    exe = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    if not exe:
        raise SystemExit("chrome.exe introuvable : adapte CHROME_PATHS dans le script.")
    print("Lancement de Chrome...")
    subprocess.Popen(
        [exe, "--remote-debugging-port=9222", f"--user-data-dir={PROFILE_DIR}", URL]
    )
    for _ in range(30):
        if cdp_ready():
            time.sleep(4)
            return
        time.sleep(1)
    raise SystemExit("Chrome n'a pas ouvert le port 9222.")


# ---------- Helpers ----------
def btn(page, pattern):
    return page.locator("button, a").filter(has_text=re.compile(pattern, re.I)).first


def clickable(loc):
    try:
        return loc.is_visible() and loc.is_enabled()
    except Exception:
        return False


def safe_click(loc, timeout=4000):
    try:
        loc.click(timeout=timeout)
        return True
    except Exception as e:
        tlog("  clic raté :", str(e).splitlines()[0])
        return False


def label(loc):
    try:
        return loc.get_attribute("aria-label", timeout=1000) or ""
    except Exception:
        return ""


def attr(loc, name):
    try:
        return loc.get_attribute(name, timeout=1000) or ""
    except Exception:
        return ""


def power_of(text):
    m = re.search(r"(\d+)\s*Power", text or "", re.I)
    return int(m.group(1)) if m else None


def cost_of(text):
    m = re.search(r",\s*(\d+)\s*RAM", text or "", re.I)
    return int(m.group(1)) if m else 0


def name_of(text):
    t = re.sub(r"^(Attack with|Target|Inspect|Select)\s+", "", text or "", flags=re.I)
    return re.split(r"\s*[,·]\s*\d", t)[0][:40]


def in_game(page):
    try:
        return bool(IN_GAME_RE.search(page.inner_text("body")))
    except Exception:
        return False


def latest_page(ctx):
    pages = [pg for pg in ctx.pages if not pg.is_closed()]
    return pages[-1] if pages else ctx.new_page()


def pause(a=0.8, b=2.0):
    time.sleep(random.uniform(a, b) * PAUSE_SCALE)


def snippet(page, n=400):
    try:
        return " ".join(page.inner_text("body").split())[:n]
    except Exception:
        return "?"


def close_overlays(page):
    """Ferme une fenêtre d'inspection de carte ouverte par erreur."""
    try:
        if page.locator("dialog.game-dialog").first.is_visible():
            page.keyboard.press("Escape")
            pause(0.3, 0.6)
    except Exception:
        pass


def close_hand_sheet(page):
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


def turn_number(page):
    try:
        txt = page.locator(".cb-crossbar").first.inner_text(timeout=1000)
    except Exception:
        return None
    m = re.search(r"TURN\s*0*(\d+)", txt, re.I)
    return int(m.group(1)) if m else None


# ---------- Logique de jeu ----------
class TurnState:
    def __init__(self, turn):
        self.turn = turn
        self.tried = set()   # cartes de la main déjà essayées ce tour
        self.played = 0      # Personnages posés ce tour
        self.done = set()    # combattants déjà traités ce tour
        self.logged = False  # état du plateau déjà affiché ce tour
        self.belief = None   # P(mon coup passe | marge m), m = 0..3, d'après la main adverse supposée
        self.belief_done = False
        self.margin = 0      # marge de puissance retenue ce tour


def try_deploy(page, ts, reserve=0, keep_support=True):
    """Pose un Personnage (ou joue un Protocole de pioche). Garde `reserve` RAM prête et au moins
    KEEP_SUPPORT Protocole de soutien en main pour défendre. Retourne True si une action a été faite."""
    if ts.played >= MAX_DEPLOY_PER_TURN:
        return False
    ram = ready_ram(page)
    n_support = len(hand_supports(page)) if (USE_RESERVE and keep_support) else 99
    cards = page.locator(SEL_HAND)
    try:
        count = cards.count()
    except Exception:
        return False

    items = []
    for i in range(count):
        card = cards.nth(i)
        lab = label(card)
        art = attr(card, "data-hand-art")
        key = art or lab
        if not lab or key in ts.tried:
            continue
        info = CARD_SCORES.get(art, {})
        # Protocole sans cible sûre : on ne le joue pas
        if info.get("kind") == "P" and not info.get("safe_protocol") and not PLAY_PROTOCOLS:
            ts.tried.add(key)
            continue
        # on garde la RAM de soutien, et le dernier Protocole de soutien pour la défense
        if ram is not None and cost_of(lab) > ram - reserve:
            continue
        if info.get("kind") == "P" and info.get("power", 0) >= 1 and n_support <= KEEP_SUPPORT:
            continue
        items.append((info.get("score", 0), cost_of(lab), i, key, lab, info.get("safe_protocol", False)))
    items.sort(reverse=True)  # meilleure carte d'abord (puissance), puis la plus chère

    for score, cost, i, key, lab, safe in items:
        ts.tried.add(key)
        if not safe_click(cards.nth(i)):
            continue
        pause(0.5, 1.0)
        commit = page.locator(SEL_COMMIT).first
        if not clickable(commit):
            close_hand_sheet(page)
            continue
        try:
            txt = commit.inner_text().strip()
        except Exception:
            txt = "?"
        if re.search(r"execute", txt, re.I) and not (PLAY_PROTOCOLS or safe):
            close_hand_sheet(page)
            continue
        if safe_click(commit):
            ts.played += 1
            ts.tried.discard(key)  # un 2e exemplaire de la même carte reste jouable
            tlog(f"  {txt} : {name_of(lab)} ({cost} RAM)")
            pause(1.5, 2.5)
            return True
        close_hand_sheet(page)
    return False


def wait_targets(page, tries=8):
    for _ in range(tries):
        try:
            if page.locator(SEL_TARGETS).count() > 0:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def pick_target(page, my_power, turn):
    """Choisit la meilleure cible parmi celles proposées. Retourne (locator, description) ou (None, '')."""
    els = page.locator(SEL_TARGETS)
    leader = None
    best_char = None
    try:
        n = els.count()
    except Exception:
        n = 0
    for i in range(n):
        el = els.nth(i)
        lab = label(el)
        pw = power_of(lab)
        if pw is None:
            continue
        if attr(el, "data-fighter") == "leader":
            leader = (el, pw, lab)
        elif pw <= my_power and (best_char is None or pw > best_char[1]):
            best_char = (el, pw, lab)

    bonus = 1 if turn in LEADER_BONUS_TURNS else 0
    if leader and my_power >= leader[1] + bonus:
        return leader[0], f"Leader adverse (défense {leader[1] + bonus})"
    if best_char:
        return best_char[0], f"KO {name_of(best_char[2])} ({best_char[1]} Power)"
    return None, ""


JS_READY_RAM = r"""
() => {
  const out = [];
  document.querySelectorAll('*').forEach(el => {
    const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    const m = t.match(/^\D{0,3}(\d+)\s*\/\s*(\d+)\s*RAM$/i);
    if (m) { const r = el.getBoundingClientRect(); out.push({y: r.top, ready: +m[1], total: +m[2]}); }
  });
  return out;
}
"""


def ram_counters(page):
    """(RAM prête chez moi, RAM prête chez l'adversaire). Moi = compteur le plus bas de l'écran."""
    try:
        items = page.evaluate(JS_READY_RAM)
    except Exception:
        return None, None
    if not items:
        return None, None
    mine = max(items, key=lambda x: x["y"])["ready"]
    ys = [i["y"] for i in items]
    opp = min(items, key=lambda x: x["y"])["ready"] if max(ys) - min(ys) > 40 else None
    return mine, opp


def ready_ram(page):
    """RAM prête de mon côté (le compteur du bas de l'écran), ou None si illisible."""
    return ram_counters(page)[0]


def leader_need(page, turn):
    """Puissance minimale pour toucher le Leader adverse ce tour (une égalité touche), sans marge."""
    opp_def = power_of(label(page.locator(SEL_OPP_LEADER).first)) or 5
    return opp_def + (1 if turn in LEADER_BONUS_TURNS else 0)


def attach_to(page, fid, who, att, want):
    """Attache de la RAM à ce combattant jusqu'à `want` de puissance. Retourne le nombre attaché."""
    sel = page.locator(".bt-attach-controls select").first
    try:
        if sel.count() > 0:
            opts = [o.strip().lower() for o in sel.locator("option").all_inner_texts()]
            key = "leader" if fid == "leader" else who.lower()
            idx = next((i for i, o in enumerate(opts) if key in o), None)
            if idx is None:
                tlog(f"  {who} : absent de la liste d'attache de RAM")
                return 0
            sel.select_option(index=idx)
            pause(0.3, 0.5)
        elif fid != "leader":
            tlog(f"  {who} : pas de liste pour choisir à qui attacher la RAM")
            return 0
    except Exception as e:
        tlog("  Choix du combattant à booster impossible :", str(e).splitlines()[0])
        return 0
    attach = page.locator(SEL_ATTACH).first
    n = 0
    while (power_of(label(att)) or 0) < want and clickable(attach) and n < 12:
        if not safe_click(attach):
            break
        n += 1
        pause(0.3, 0.6)
    return n


def attack_with(page, fid, ts, need, budget, margin=0):
    """Fait attaquer le combattant fid, en le boostant avec de la RAM si ça permet de toucher
    le Leader adverse. Retourne True (une action a eu lieu)."""
    ts.done.add(fid)
    att = page.locator(f'button[data-seat="0"][data-fighter="{fid}"]').first
    who = name_of(label(att)) or fid

    power = power_of(label(att)) or 0
    extra0 = max(0, need - power)
    can_pay = budget is None or extra0 <= budget
    # marge : on ajoute de la RAM au-delà du minimum seulement si le budget le permet
    m_eff = margin if budget is None else (max(0, min(margin, budget - extra0)) if can_pay else 0)
    want = need + m_eff
    if can_pay and want > power:
        n = attach_to(page, fid, who, att, want)
        if n:
            tlog(f"  RAM attachée à {who} : {n}" + (f" (dont marge +{m_eff})" if m_eff else ""))
    my_power = power_of(label(att)) or 0

    if not safe_click(att):
        close_overlays(page)
        return True
    if not wait_targets(page):
        safe_click(att)  # le 1er clic n'a peut-être rien sélectionné
        if not wait_targets(page):
            tlog(f"  {who} : aucune cible proposée")
            page.screenshot(path="attack_fail.png")
            close_overlays(page)
            return True

    target, desc = pick_target(page, my_power, ts.turn or 0)
    if target is None:
        tlog(f"  {who} ({my_power}) n'attaque pas : aucune cible rentable (il faut {need} pour le Leader)")
        try:
            page.keyboard.press("Escape")
            pause(0.3, 0.6)
            if page.locator(SEL_TARGETS).count() > 0:
                safe_click(att)  # désélection
                pause(0.3, 0.6)
        except Exception:
            pass
        return True

    if not safe_click(target):
        close_overlays(page)
        return True
    pause(0.4, 0.8)

    go = (
        page.locator(".bt-combat button.bt-primary")
        .filter(has_text=re.compile(r"^\s*attack\s*$", re.I))
        .first
    )
    on_leader = desc.startswith("Leader")
    before = hp_of(label(page.locator(SEL_OPP_LEADER).first)) if on_leader else None
    if not safe_click(go):
        tlog(f"  {who} : bouton ATTACK introuvable")
        page.screenshot(path="attack_fail.png")
        close_overlays(page)
        return True
    GS["attacks"] += 1
    outcome = ""
    if on_leader:
        after = hp_after(page, SEL_OPP_LEADER, before)
        hit = (after is not None and before is not None and after < before) or (after is None and before == 1)
        if hit:
            GS["leader_hits"] += 1
            outcome = " -> touché"
        else:
            GS["leader_blocked"] += 1
            outcome = " -> bloqué"
        # calibration de la croyance : coup touché selon la marge réellement utilisée
        margin_used = max(0, my_power - need)
        key = "m0" if margin_used == 0 else "m1"
        GS[key + "_att"] += 1
        GS[key + "_hit"] += int(hit)
        if ts.belief:
            GS["pred_sum"] += ts.belief[min(margin_used, 3)]
            GS["pred_n"] += 1
    tlog(f"  ATTACK : {who} ({my_power}) -> {desc}{outcome}")
    pause(1, 2)
    return True


def card_value_by_name(token):
    """Valeur d'une carte désignée par son nom (même partiel, ex. « Daniel »), d'après card_scores.json."""
    token = token.strip().lower()
    best = 0.0
    if not token:
        return best
    for info in CARD_SCORES.values():
        name = (info.get("name") or "").lower()
        if name and token in name:
            v = info.get("score", 0.0)
            if info.get("kind") == "P" and v <= 0:
                v = 0.2
            best = max(best, v)
    return best


def handle_scan(page):
    """Invite « Private Scan · choose the top card » : on place la meilleure carte au sommet du deck
    (puissance la plus haute), puis KEEP THIS ORDER. Retourne True si l'invite a été traitée."""
    keep = btn(page, r"keep this order")
    if not clickable(keep):
        return False
    try:
        sel = keep.locator("xpath=ancestor::*[.//select][1]").locator("select").first
        if sel.count() > 0:
            opts = [o.strip() for o in sel.locator("option").all_inner_texts()]
            best_i, best_v = None, -1.0
            for i, o in enumerate(opts):
                v = card_value_by_name(re.split(r"\s*(?:→|->)\s*", o)[0])
                if v > best_v:
                    best_i, best_v = i, v
            cur = sel.evaluate("e => e.selectedIndex")
            if best_i is not None and best_i != cur:
                sel.select_option(index=best_i)
                pause(0.3, 0.6)
            tlog(f"  Scan : ordre retenu « {opts[best_i] if best_i is not None else '?'} »")
    except Exception as e:
        tlog("  Scan : ordre par défaut conservé (", str(e).splitlines()[0], ")")
    safe_click(keep)
    return True


# ---------- Navigation hors partie ----------
START_RE = r"^\s*(practice|start training)\s*$"   # « Practice » depuis la mise à jour du site


def find_start(page):
    """Bouton qui lance une partie d'entraînement dans l'Arena."""
    return btn(page, START_RE)


def go_to_arena(page):
    """Depuis un écran hors partie (résultat, accueil...), va vers le lobby de l'Arena.
    Retourne True si un clic a été fait."""
    candidates = [
        btn(page, r"back to arena"),                        # écran de résultat
        page.locator("button.quick-arena").first,          # « DUEL ARENA » sur l'accueil
        page.get_by_role("link", name=re.compile(r"^\s*arena\s*$", re.I)).first,   # menu du haut
        btn(page, r"^\s*hub\s*$"),                         # ancien bouton de retour
        page.locator("a.brand").first,                     # logo : retour à l'accueil
    ]
    for loc in candidates:
        if clickable(loc):
            try:
                loc.click(timeout=4000)
                return True
            except Exception:
                continue
    return False


# ---------- Statistiques et aides à la décision ----------
GS = {}
STATS_FIELDS = ["time", "game", "deck", "config", "result", "turns", "seat", "attacks", "leader_hits",
                "leader_blocked", "hits_taken", "defenses_blocked", "supports", "guards",
                "m0_att", "m0_hit", "m1_att", "m1_hit", "pred_hit"]


def reset_stats():
    GS.clear()
    GS.update({"attacks": 0, "leader_hits": 0, "leader_blocked": 0, "taken": 0,
               "def_blocked": 0, "supports": 0, "guards": 0,
               "m0_att": 0, "m0_hit": 0, "m1_att": 0, "m1_hit": 0, "pred_sum": 0.0, "pred_n": 0})


reset_stats()


def config_tag():
    return (f"reserve={int(USE_RESERVE)} guardhome={GUARD_HOME_HP} "
            f"margin={'auto' if MARGIN_AUTO else ATTACK_MARGIN} keep={KEEP_SUPPORT}")


def rotate_old_stats():
    """Si stats.csv a l'ancien format de colonnes, on le range à part pour ne pas mélanger les lignes."""
    try:
        with open(STATS_PATH, encoding="utf-8") as f:
            header = f.readline().strip()
    except Exception:
        return
    if header != ",".join(STATS_FIELDS):
        try:
            os.replace(STATS_PATH, STATS_PATH.replace(".csv", time.strftime("_ancien_%m%d-%H%M.csv")))
        except Exception:
            pass


def write_stats(game, result, turns, seat="?"):
    """Ajoute une ligne à stats.csv et affiche le bilan de la partie."""
    rotate_old_stats()
    new = not os.path.exists(STATS_PATH)
    row = {
        "time": time.strftime("%Y-%m-%d %H:%M"), "game": game, "deck": ACTIVE_DECK_NAME or "?",
        "config": config_tag(), "result": result, "turns": turns, "seat": seat,
        "m0_att": GS["m0_att"], "m0_hit": GS["m0_hit"], "m1_att": GS["m1_att"], "m1_hit": GS["m1_hit"],
        "pred_hit": (round(GS["pred_sum"] / GS["pred_n"], 3) if GS["pred_n"] else ""),
        "attacks": GS["attacks"], "leader_hits": GS["leader_hits"],
        "leader_blocked": GS["leader_blocked"], "hits_taken": GS["taken"],
        "defenses_blocked": GS["def_blocked"], "supports": GS["supports"],
        "guards": GS["guards"],
    }
    try:
        with open(STATS_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=STATS_FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print("Écriture de stats.csv impossible :", str(e).splitlines()[0])
    tlog(f"Bilan : {result} en {turns} tours | coups au Leader adverse : "
          f"{GS['leader_hits']} touchés / {GS['leader_blocked']} bloqués | "
          f"coups encaissés : {GS['taken']} | bloqués : {GS['def_blocked']} "
          f"(soutiens {GS['supports']}, Guards {GS['guards']})")


def wilson(k, n, z=1.96):
    """Intervalle de confiance à 95 % d'une proportion (Wilson)."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def two_prop_p(k1, n1, k2, n2):
    """p-valeur (test z bilatéral) de l'écart entre deux taux de victoire."""
    if n1 == 0 or n2 == 0:
        return 1.0
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = (k1 / n1 - k2 / n2) / se
    return 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))


def games_needed(p, delta=0.10):
    """Parties par réglage pour détecter un gain de `delta` (puissance 80 %, seuil 5 %)."""
    p1, p2 = p, min(0.99, p + delta)
    return math.ceil((1.96 + 0.84) ** 2 * (p1 * (1 - p1) + p2 * (1 - p2)) / delta ** 2)


def report_stats():
    """Bilan de stats.csv par réglage, avec intervalles de confiance et comparaison des réglages."""
    try:
        with open(STATS_PATH, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        print(f"Aucune statistique ({STATS_PATH} absent) : joue quelques parties d'abord.")
        return
    groups = {}
    for r in rows:
        groups.setdefault(f"{r.get('deck') or '?'} | {r['config']}", []).append(r)

    def num(r, k):
        try:
            return float(r.get(k) or 0)
        except ValueError:
            return 0.0

    def avg(rs, k):
        vals = [float(r[k]) for r in rs if r.get(k) not in (None, "")]
        return sum(vals) / len(vals) if vals else 0.0

    print(f"{len(rows)} parties dans {STATS_PATH}\n")
    results = {}
    for cfg, rs in groups.items():
        wins = sum(1 for r in rs if r["result"] == "win")
        lo, hi = wilson(wins, len(rs))
        results[cfg] = (wins, len(rs))
        print(f"[{cfg}]")
        print(f"  {len(rs)} parties, {wins} victoires ({100 * wins / len(rs):.0f} %, "
              f"intervalle à 95 % : {100 * lo:.0f}-{100 * hi:.0f} %), {avg(rs, 'turns'):.1f} tours en moyenne")
        for seat, name in (("first", "premier joueur"), ("second", "second joueur")):
            sr = [r for r in rs if r.get("seat") == seat]
            if sr:
                w = sum(1 for r in sr if r["result"] == "win")
                print(f"    {name} : {w}/{len(sr)} victoires ({100 * w / len(sr):.0f} %)")
        print(f"  par partie : {avg(rs, 'leader_hits'):.1f} coups touchés au Leader adverse, "
              f"{avg(rs, 'hits_taken'):.1f} coups encaissés, {avg(rs, 'defenses_blocked'):.1f} bloqués "
              f"({avg(rs, 'supports'):.1f} soutiens, {avg(rs, 'guards'):.1f} Guards)")
        m0a, m0h = sum(num(r, "m0_att") for r in rs), sum(num(r, "m0_hit") for r in rs)
        m1a, m1h = sum(num(r, "m1_att") for r in rs), sum(num(r, "m1_hit") for r in rs)
        if m0a or m1a:
            print("  coups au Leader selon la marge : "
                  + (f"sans marge {int(m0h)}/{int(m0a)} touchés ({100 * m0h / m0a:.0f} %)" if m0a else "sans marge : aucun")
                  + " | "
                  + (f"avec marge {int(m1h)}/{int(m1a)} ({100 * m1h / m1a:.0f} %)" if m1a else "avec marge : aucun"))
        preds = [float(r["pred_hit"]) for r in rs if r.get("pred_hit") not in (None, "")]
        if preds:
            print(f"  probabilité de toucher prédite par la croyance : {100 * sum(preds) / len(preds):.0f} % en moyenne")
        print()

    cfgs = [c for c in results if results[c][1] >= 5]
    for i in range(len(cfgs)):
        for j in range(i + 1, len(cfgs)):
            (k1, n1), (k2, n2) = results[cfgs[i]], results[cfgs[j]]
            pv = two_prop_p(k1, n1, k2, n2)
            verdict = "écart significatif" if pv < 0.05 else "écart NON significatif (trop peu de parties ou réglages équivalents)"
            print(f"Comparaison [{cfgs[i]}] vs [{cfgs[j]}] : p = {pv:.2f} -> {verdict}")
    if results:
        biggest = max(results.values(), key=lambda x: x[1])
        p0 = biggest[0] / biggest[1] if biggest[1] else 0.5
        print(f"\nPour détecter un gain de +10 points de victoires, il faut environ "
              f"{games_needed(max(0.05, min(0.9, p0)))} parties par réglage.")


def hp_after(page, sel, before, tries=8):
    """PV lus sur le Leader `sel` une fois l'animation terminée (None si l'écran a changé)."""
    for _ in range(tries):
        try:
            if page.locator(sel).count() == 0:
                return None
        except Exception:
            return None
        h = hp_of(label(page.locator(sel).first))
        if h is not None and before is not None and h != before:
            return h
        time.sleep(0.35)
    try:
        return hp_of(label(page.locator(sel).first)) if page.locator(sel).count() else None
    except Exception:
        return None


def hand_supports(page):
    """Protocoles de la main utilisables en soutien (puissance >= 1) : liste de (art, coût, bonus)."""
    out = []
    cards = page.locator('button.ah-card[data-hand-owner="self"]')
    try:
        n = cards.count()
    except Exception:
        return out
    for i in range(n):
        art = attr(cards.nth(i), "data-hand-art")
        info = CARD_SCORES.get(art, {})
        if info.get("kind") == "P" and info.get("power", 0) >= 1:
            out.append((art, info.get("ram", 0), info.get("power", 0)))
    return out


def support_reserve(page):
    """RAM à garder prête pour payer le soutien le moins cher que j'ai en main."""
    if not USE_RESERVE:
        return 0
    sup = hand_supports(page)
    return min((c for _, c, _ in sup), default=0)


def lethal_possible(cands, need, ram, opp_hp):
    """Vrai si, avec toute la RAM, assez de combattants peuvent atteindre `need` pour achever le Leader."""
    if opp_hp is None or ram is None or not cands:
        return False
    hits = 0
    spent = 0
    for extra in sorted(max(0, need - pw) for _, pw in cands):
        if spent + extra <= ram:
            hits += 1
            spent += extra
    return hits >= opp_hp


# ---------- Croyance par comptage sur la main adverse ----------
MIRROR_POOL = []   # cartes du deck adverse supposé (miroir du mien en entraînement)


def load_mirror_pool():
    """En « Practice », l'adversaire joue un miroir de mon deck (texte de l'écran Arena) : sa liste
    est donc connue. On prend le dernier deck appliqué (sinon le deck recommandé). Retourne la taille."""
    global MIRROR_POOL
    st = load_state()
    deck = st.get("applied") or st.get("deck") or {}
    pool = []
    for art, n in deck.items():
        pool += [art] * int(n)
    MIRROR_POOL = pool
    return len(pool)


def opp_ready_ram(page):
    return ram_counters(page)[1]


def opp_hand_size(page):
    """Cartes en main de l'adversaire (« 5 IN HAND » affiché sous son éventail)."""
    try:
        txt = page.inner_text("body")
    except Exception:
        return None
    m = re.search(r"(\d+)\s*IN HAND", txt, re.I)
    return int(m.group(1)) if m else None


def support_table():
    """art -> (coût RAM, bonus) des Protocoles pouvant servir de soutien (puissance >= 1)."""
    return {art: (info.get("ram", 0), info.get("power", 0)) for art, info in CARD_SCORES.items()
            if info.get("kind") == "P" and info.get("power", 0) >= 1}


def block_probabilities(pool, hand_size, opp_ram, n=BELIEF_SAMPLES):
    """Tire `n` mains adverses compatibles (sans remise dans le deck connu) et retourne, pour une
    marge m = 0..3 de puissance au-dessus de la défense, la probabilité que mon coup PASSE :
    il passe si le meilleur soutien que l'adversaire peut payer apporte au plus m."""
    sup = support_table()
    if not pool or hand_size is None or opp_ram is None or not sup:
        return None
    k = min(hand_size, len(pool))
    counts = [0] * 5
    for _ in range(n):
        best = 0
        for art in random.sample(pool, k):
            c = sup.get(art)
            if c and c[0] <= opp_ram and c[1] > best:
                best = c[1]
        counts[min(best, 4)] += 1
    out, cum = [], 0
    for m in range(4):
        cum += counts[m]
        out.append(cum / n)
    return out


def belief_for_turn(page):
    """Probabilités de coup réussi selon la marge, ou None si la croyance est indisponible.
    Limite : on retire les cartes visibles sur son plateau, mais pas celles déjà défaussées."""
    if not MIRROR_POOL:
        return None
    pool = list(MIRROR_POOL)
    try:
        pieces = page.locator('button.cb-piece[data-seat="1"]')
        for i in range(pieces.count()):
            art = attr(pieces.nth(i), "data-art")
            if art in pool:
                pool.remove(art)
    except Exception:
        pass
    return block_probabilities(pool, opp_hand_size(page), opp_ready_ram(page))


def choose_margin(probs):
    """Marge m qui maximise P(coup passe) - MARGIN_RAM_COST * m (chaque point de marge coûte 1 RAM)."""
    if not probs:
        return ATTACK_MARGIN
    best_m, best_v = 0, probs[0]
    for m in range(1, 4):
        v = probs[m] - MARGIN_RAM_COST * m
        if v > best_v + 1e-9:
            best_m, best_v = m, v
    return best_m


# ---------- Défense ----------
DEFENSE_DEBUG_DONE = False


def hp_of(text):
    m = re.search(r"(\d+)\s*HP", text or "", re.I)
    return int(m.group(1)) if m else None


def parse_support_option(text):
    """(coût RAM, bonus de puissance) d'une option de soutien Protocole, ou None."""
    low = text.lower()
    if not text.strip() or "no support" in low:
        return None
    cost = bonus = None
    for info in CARD_SCORES.values():
        if info.get("kind") == "P" and info.get("name") and info["name"].lower() in low:
            cost, bonus = info.get("ram", 0), info.get("power", 0)
            break
    m = re.search(r"\+\s*(\d+)", text)
    if m:
        bonus = int(m.group(1))
    m = re.search(r"(\d+)\s*RAM", text, re.I)
    if m:
        cost = int(m.group(1))
    if bonus is None:
        return None
    return (cost or 0), bonus


def guard_option_power(text):
    """Puissance d'un Personnage proposé pour intercepter, ou None."""
    low = text.lower()
    if not text.strip() or re.search(r"\b(no|none|aucun)\b", low):
        return None
    m = re.search(r"(\d+)\s*Power", text, re.I)
    if m:
        return int(m.group(1))
    for info in CARD_SCORES.values():
        if info.get("kind") == "C" and info.get("name") and info["name"].lower() in low:
            return info.get("power", 0)
    return None


def panel_numbers(panel):
    try:
        txt = " ".join(panel.inner_text(timeout=1500).split())
    except Exception:
        return None, None, ""
    m = re.search(r"(\d+)\s*vs\.?\s*(\d+)", txt, re.I)
    return (int(m.group(1)), int(m.group(2)), txt) if m else (None, None, txt)


def dump_defense_debug(panel):
    """Écrit une fois defense_debug.json : contenu de l'écran de défense (pour l'ajuster)."""
    global DEFENSE_DEBUG_DONE
    if DEFENSE_DEBUG_DONE:
        return
    DEFENSE_DEBUG_DONE = True
    try:
        info = {
            "text": " ".join(panel.inner_text(timeout=1500).split()),
            "selects": panel.locator("select").evaluate_all(
                "els => els.map(e => [...e.options].map(o => o.text))"),
            "buttons": panel.locator("button").evaluate_all(
                "els => els.map(e => (e.getAttribute('aria-label') || e.innerText || '').trim())"),
            "html": panel.evaluate("e => e.outerHTML.slice(0, 6000)"),
        }
        with open("defense_debug.json", "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=1)
        tlog("  (écran de défense enregistré dans defense_debug.json)")
    except Exception:
        pass


def defend(page, resolve):
    """Décide de la défense : Guard qui bloque, sinon soutien Protocole qui fait passer la
    défense strictement au-dessus de l'attaque, sinon sacrifice d'un Guard si les PV sont bas."""
    panel = page.locator(".cb-defense").first
    A, D, txt = panel_numbers(panel)
    dump_defense_debug(panel)
    if A is None:
        tlog("Resolve defense (puissances illisibles :", txt[:90], ")")
        safe_click(resolve)
        return
    ram = ready_ram(page)
    hp = hp_of(label(page.locator(SEL_MY_LEADER).first))
    tlog(f"Défense : attaque {A} vs {D} (RAM prête : {ram}, PV : {hp})")

    def finish():
        safe_click(resolve)
        after = hp_after(page, SEL_MY_LEADER, hp)
        if (after is not None and hp is not None and after < hp) or (after is None and hp == 1):
            GS["taken"] += 1
            tlog("  -> coup encaissé")
        else:
            GS["def_blocked"] += 1
            tlog("  -> coup bloqué")

    if A < D:
        tlog("  le coup ne passe pas")
        finish()
        return

    # sépare la liste de soutien (« No support ») des autres listes (interception)
    support, others = None, []
    try:
        sels = panel.locator("select")
        for i in range(sels.count()):
            sel = sels.nth(i)
            opts = [o.strip() for o in sel.locator("option").all_inner_texts()]
            if any("no support" in o.lower() for o in opts):
                support = (sel, opts)
            else:
                others.append((sel, opts))
    except Exception:
        pass

    # 1) interception : un Guard plus fort que l'attaque bloque pour de bon ;
    #    sinon, PV bas : on sacrifie le Guard le plus faible (il est KO à la place du Leader)
    chumped = False
    for sel, opts in others:
        cands = [(guard_option_power(o), i, o) for i, o in enumerate(opts)]
        cands = [c for c in cands if c[0] is not None]
        if not cands:
            continue
        blockers = sorted(c for c in cands if c[0] > A)
        pick = None
        chump = False
        if blockers:
            pick = blockers[0]
        elif hp is not None and hp <= INTERCEPT_CHUMP_HP:
            pick = min(cands)
            chump = True
        if pick:
            try:
                sel.select_option(index=pick[1])
                tlog(f"  interception : {pick[2]} ({pick[0]} Power)"
                     + (" - sacrifié à la place du Leader" if chump else ""))
                chumped = chump
                GS["guards"] += 1
                pause(0.4, 0.8)
                A2, D2, _ = panel_numbers(panel)
                if A2 is not None:
                    A, D = A2, D2
            except Exception as e:
                tlog("  interception impossible :", str(e).splitlines()[0])
            break

    # 2) soutien Protocole : le moins cher qui fait passer la défense au-dessus de l'attaque
    if A >= D and support and not chumped:
        sel, opts = support
        need = A - D + 1
        cands = []
        for i, o in enumerate(opts):
            parsed = parse_support_option(o)
            if not parsed:
                continue
            cost, bonus = parsed
            if bonus >= need and (ram is None or cost <= ram):
                cands.append((cost, bonus, i, o))
        if cands:
            cost, bonus, i, o = min(cands)
            try:
                sel.select_option(index=i)
                tlog(f"  soutien : {o} (+{bonus}, {cost} RAM)")
                GS["supports"] += 1
                pause(0.4, 0.8)
            except Exception as e:
                tlog("  soutien impossible :", str(e).splitlines()[0])
        else:
            tlog(f"  aucun soutien ne suffit (il faut +{need}) : le coup passe")
    elif A >= D and not chumped:
        tlog("  pas de défense possible : le coup passe")
    finish()


def my_turn(page, ts):
    """Retourne True si une action a été faite (on reboucle), False si on peut finir le tour.
    Ordre : 1) attaquer (le moins cher à booster d'abord, Leader en premier à égalité),
            2) déployer avec la RAM qui reste.
    Défense : on garde une RAM de soutien et les Guards prêts, sauf si on peut achever l'adversaire."""
    close_overlays(page)
    need = leader_need(page, ts.turn or 0)
    ram = ready_ram(page)
    opp_hp = hp_of(label(page.locator(SEL_OPP_LEADER).first))
    my_hp = hp_of(label(page.locator(SEL_MY_LEADER).first))

    cand_all = []
    try:
        attackers = page.locator(SEL_MY_ATTACKERS)
        n = attackers.count()
    except Exception:
        attackers, n = None, 0
    for i in range(n):
        el = attackers.nth(i)
        fid = attr(el, "data-fighter")
        if fid and fid not in ts.done:
            lab = label(el)
            cand_all.append((fid, power_of(lab) or 0, lab))

    if not ts.belief_done:        # la croyance ne change pas pendant mon tour : on la calcule une fois
        ts.belief_done = True
        ts.belief = belief_for_turn(page)
        ts.margin = choose_margin(ts.belief) if (MARGIN_AUTO and ts.belief) else ATTACK_MARGIN

    allin = lethal_possible([(f, p) for f, p, _ in cand_all], need, ram, opp_hp)
    reserve = 0 if allin else support_reserve(page)
    budget = None if ram is None else max(0, ram - reserve)

    cand = []
    for fid, pw, lab in cand_all:
        if (fid != "leader" and "guard" in lab.lower() and not allin
                and my_hp is not None and my_hp <= GUARD_HOME_HP):
            ts.done.add(fid)
            tlog(f"  {name_of(lab)} reste prêt à intercepter (Guard, {my_hp} PV)")
            continue
        cand.append((fid, pw))

    if not ts.logged:
        ts.logged = True
        try:
            pieces = page.locator('button.cb-piece[data-seat="0"]')
            mine = [(label(pieces.nth(i))[:55], "prêt" if "cb-pickable" in attr(pieces.nth(i), "class") else "-")
                    for i in range(pieces.count())]
        except Exception:
            mine = []
        tlog(f"  RAM prête : {ram} (réserve défense : {reserve}) | PV : moi {my_hp} / adversaire {opp_hp} "
              f"| il faut {need} Power pour toucher le Leader adverse" + (" | ACHÈVEMENT POSSIBLE" if allin else ""))
        if ts.belief:
            tlog("  Croyance sur la main adverse : P(coup passe | marge 0/1/2/3) = "
                 + " / ".join(f"{100 * p:.0f}%" for p in ts.belief) + f" -> marge retenue : +{ts.margin}")
        tlog(f"  Attaquants disponibles : {[(f, p) for f, p, _ in cand_all]}")
        if mine:
            tlog(f"  Mes Personnages sur le plateau : {mine}")

    if cand:
        def key(c):
            fid, pw = c
            extra = max(0, need - pw)
            affordable = budget is None or extra <= budget
            return (not affordable, extra, -pw, fid != "leader")
        cand.sort(key=key)
        return attack_with(page, cand[0][0], ts, need, budget, ts.margin)

    return try_deploy(page, ts, reserve, keep_support=not allin)


# =====================================================================
# 1. BASE DE CARTES : mise à jour incrémentale de cards_db.json
# =====================================================================
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


def load_all(page):
    """Fait défiler la collection jusqu'à ce que le nombre de cartes ne bouge plus."""
    stable = 0
    last = -1
    for _ in range(60):
        page.mouse.wheel(0, 3000)
        time.sleep(0.5)
        c = page.locator("button[data-inspect-art]").count()
        if c == last:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
            last = c
    page.evaluate("window.scrollTo(0, 0)")


def dialog_loc(page):
    return page.locator('dialog[open], [role="dialog"]').first


def read_dialog(page):
    dlg = dialog_loc(page)
    dlg.wait_for(state="visible", timeout=5000)
    time.sleep(0.4)
    return dlg.evaluate(JS_DIALOG)


def close_dialog(page):
    page.keyboard.press("Escape")
    time.sleep(0.4)
    try:
        if dialog_loc(page).is_visible():
            x = dialog_loc(page).locator("button[aria-label*='lose' i]").first
            if x.is_visible():
                x.click(timeout=2000)
                time.sleep(0.4)
    except Exception:
        pass


def load_db():
    try:
        with open(DB_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_db(entries):
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)


def update_db(page, force=False):
    """Compare la collection affichée à cards_db.json et n'ouvre que les cartes nouvelles
    ou dont le nombre d'exemplaires a changé. Retourne True si la collection a changé
    (le RAM Module #060 est ignoré pour ça)."""
    first_run = not os.path.exists(DB_PATH)
    old = {e["art"]: e for e in load_db() if "art" in e}
    print("Mise à jour de la base de cartes...")
    page.goto(URL + "collection")
    time.sleep(5)
    load_all(page)
    tiles = page.evaluate(JS_TILES)
    if not tiles:
        print("  Aucune carte lue sur la page collection : base conservée telle quelle.")
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
        print(f"  Base à jour ({len(tiles)} cartes), rien à ouvrir.")
    else:
        print(f"  {len(todo)} carte(s) à ouvrir sur {len(tiles)}.")
    buttons = page.locator("button[data-inspect-art]")
    for n, i in enumerate(todo, 1):
        t = tiles[i]
        entry = {"art": t["art"], "label": t["label"], "tile": t["tile"]}
        try:
            buttons.nth(i).click(timeout=5000)
            entry["front"] = read_dialog(page)
        except Exception as e:
            print(f"  [{n}/{len(todo)}] erreur sur {t['label']} : {str(e).splitlines()[0]}")
            close_dialog(page)
            if t["art"] in old:
                new[t["art"]] = old[t["art"]]
            continue
        close_dialog(page)
        new[t["art"]] = entry
        if t["art"] != "060":
            changed = True
        print(f"  [{n}/{len(todo)}] {t['label'].replace('Inspect ', '')}")
        if n % 10 == 0:
            save_db(list(new.values()))

    save_db(list(new.values()))
    print(f"  cards_db.json : {len(new)} cartes" + (" (changement détecté)" if changed else ""))
    return changed


# =====================================================================
# 2. OPTIMISATION DU DECK (simulation de tirages aléatoires)
# =====================================================================
def parse_cards(entries):
    cards = {}
    for c in entries:
        front = c.get("front")
        if not front:
            continue
        t = front["text"]
        art = c["art"]
        if "RESOURCE VALUE" in t:        # RAM Module : fourni à part
            continue
        name = re.search(r"Inspect (.+), [A-Za-z ]+$", c["label"])
        name = name.group(1) if name else c["label"]
        color = re.search(r"COLOR\n(\w+)", t)
        power = re.search(r"POWER\n(\d+)", t)
        ram = re.search(r"RAM\n(\d+)", t)
        hp = re.search(r"HP\n(\d+)", t)
        owned = re.search(r"×(\d+)", c["tile"])
        ability = t.split("ABILITY")[1:] if "ABILITY" in t else []
        text = " ".join(" ".join(a.split()) for a in ability).split("Share on X")[0]
        m = re.match(r"\d+", art)
        n = int(m.group(0)) if m else 0
        if hp is not None and ram is None:
            kind = "L"
        elif any(a <= n <= b for a, b in PROTOCOL_RANGES):
            kind = "P"
        else:
            kind = "C"
        cards[art] = {
            "art": art, "name": name, "kind": kind,
            "color": color.group(1) if color else "?",
            "power": int(power.group(1)) if power else 0,
            "ram": int(ram.group(1)) if ram else 0,
            "owned": int(owned.group(1)) if owned else 1,
            "guard": "Guard" in text,
            "rush": "Rush" in text,
            "text": text,
        }
    return cards


def card_value(ch):
    """Valeur d'un Personnage posé : puissance, +bonus Guard (défense) et Rush (attaque immédiate)."""
    return ch["power"] + (1.0 if ch["guard"] else 0.0) + (1.5 if ch["rush"] else 0.0)


def best_deploy(hand_chars, capacity, slots, leader_refund):
    """Meilleur sous-ensemble de Personnages à poser avec `capacity` RAM (DP sur le coût).
    leader_refund : le Leader rend 1 RAM au premier Personnage de coût >= 4 (Jensen Huang)."""
    def knap(cards, cap, free):
        dp = [(0.0, ())] * (cap + 1)
        for idx, ch in cards:
            new = dp[:]
            for c in range(ch["ram"], cap + 1):
                if len(dp[c - ch["ram"]][1]) >= free:
                    continue
                v = dp[c - ch["ram"]][0] + card_value(ch)
                if v > new[c][0]:
                    new[c] = (v, dp[c - ch["ram"]][1] + (idx,))
            dp = new
        return max(dp, key=lambda x: x[0])

    cards = list(enumerate(hand_chars))
    best = knap(cards, capacity, slots)
    if leader_refund:
        for idx, ch in cards:
            if 4 <= ch["ram"] <= capacity:
                rest = [(i, c) for i, c in cards if i != idx]
                sub = knap(rest, capacity - ch["ram"] + 1, slots - 1)
                v = sub[0] + card_value(ch)
                if v > best[0]:
                    best = (v, sub[1] + (idx,))
    return best[1]


def is_support(c):
    """Protocole utilisable comme soutien en défense (puissance imprimée >= 1)."""
    return c["kind"] == "P" and c["power"] >= 1


def simulate(deck, orders, leader_refund):
    """Score moyen sur les mélanges `orders` :
      - puissance cumulée prête à attaquer du tour 2 à 7 (un Personnage posé attaque au tour
        suivant ; Rush : tout de suite) ;
      - + DEFENSE_WEIGHT par coup adverse bloqué : à chaque tour adverse, si j'ai un Protocole de
        soutien en main, il fait passer une égalité sous mon avantage (il est alors consommé).
    Comme dans le vrai bot, on garde la RAM du soutien le moins cher, et on ne joue pas un
    Protocole de pioche s'il est mon seul soutien."""
    defensive = DEFENSE_WEIGHT > 0
    total = 0.0
    for s, order in enumerate(orders):
        library = [deck[i] for i in order]
        hand = library[:5]
        pos = 5
        second = s % 2 == 1                 # une partie sur deux en 2e joueur
        board = []
        score = 0.0
        for t in range(1, SIM_TURNS + 1):
            if t > 1 or second:             # le 1er joueur ne pioche pas au tour 1
                if pos < len(library):
                    hand.append(library[pos])
                    pos += 1
            ready_power = sum(card_value(c) for c in board)
            sup = [c for c in hand if is_support(c)]
            reserve = min((c["ram"] for c in sup), default=0) if defensive else 0
            cap = max(min(t, 10) - reserve, 0)
            for _ in range(3):
                chars = [c for c in hand if c["kind"] == "C"]
                free = 5 - len(board)
                chosen = best_deploy(chars, cap, free, leader_refund) if (free > 0 and chars) else ()
                if chosen:
                    for c in [chars[i] for i in chosen]:
                        hand.remove(c)
                        board.append(c)
                        cap -= c["ram"]
                        if c["rush"]:
                            ready_power += card_value(c)
                    cap = max(cap, 0)
                drew = False
                for c in list(hand):
                    if (c["kind"] == "P" and c["art"] in SAFE_PROTOCOLS and c["ram"] <= cap
                            and pos < len(library)
                            and (not defensive or not is_support(c)
                                 or sum(1 for h in hand if is_support(h)) >= 2)):
                        hand.remove(c)
                        cap -= c["ram"]
                        hand.append(library[pos])
                        pos += 1
                        drew = True
                if not chosen and not drew:
                    break
            if t >= 2:
                score += ready_power
                sup = [c for c in hand if is_support(c)]
                if defensive and sup:        # un coup adverse bloqué par mon meilleur soutien bon marché
                    hand.remove(min(sup, key=lambda c: c["ram"]))
                    score += DEFENSE_WEIGHT
        total += score
    return total / len(orders)


def deck_ok(counts, cards, leader_color):
    if sum(counts.values()) != DECK_SIZE:
        return False
    other = Counter()
    for art, n in counts.items():
        col = cards[art]["color"]
        if col != leader_color:
            other[col] += n
    return len(other) <= 1 and all(n <= SECONDARY_MAX for n in other.values())


def copy_limit(card):
    return min(MAX_COPIES, card["owned"])


def static_score(c):
    if c["kind"] == "C":
        return c["power"] / max(c["ram"], 1) ** 0.5 + (1 if c["guard"] else 0) + (1.5 if c["rush"] else 0)
    base = 2.0 if c["art"] in SAFE_PROTOCOLS else 0.3
    if DEFENSE_WEIGHT > 0 and is_support(c):
        base += 1.5 if c["ram"] == 0 else 0.7      # un soutien à 0 RAM bloque une égalité gratuitement
    return base


def initial_deck(cards, leader_color, second_color):
    pool = [c for c in cards.values() if c["kind"] in "CP" and c["color"] in (leader_color, second_color)]
    pool.sort(key=static_score, reverse=True)
    counts = Counter()
    other = 0
    for c in pool:
        for _ in range(copy_limit(c)):
            if sum(counts.values()) >= DECK_SIZE:
                break
            if c["color"] != leader_color:
                if other >= SECONDARY_MAX:
                    break
                other += 1
            counts[c["art"]] += 1
    return counts


def evaluate(counts, cards, orders, refund):
    deck = []
    for art, n in counts.items():
        deck += [cards[art]] * n
    return simulate(deck, orders, refund)


def make_orders(sims, seed=7):
    rnd = random.Random(seed)
    orders = []
    for _ in range(sims):
        o = list(range(DECK_SIZE))
        rnd.shuffle(o)
        orders.append(o)
    return orders


def optimize(cards, leader_art, iters, sims, seed=7):
    """Retourne (score, counts, couleur_secondaire) ou None si aucun deck valide n'existe."""
    rnd = random.Random(seed)
    leader = cards[leader_art]
    lcol = leader["color"]
    refund = leader["name"] == "Jensen Huang"
    orders = make_orders(sims, seed)

    colors = sorted({c["color"] for c in cards.values() if c["kind"] in "CP" and c["color"] != lcol})
    best_global = None
    for sc in colors + [None]:
        counts = initial_deck(cards, lcol, sc)
        if not deck_ok(counts, cards, lcol):
            continue
        cur = evaluate(counts, cards, orders, refund)
        candidates = [c for c in cards.values() if c["kind"] in "CP" and c["color"] in (lcol, sc)]
        for _ in range(iters):
            out = rnd.choice(list(counts.elements()))
            cin = rnd.choice(candidates)["art"]
            if cin == out or counts[cin] >= copy_limit(cards[cin]):
                continue
            new = counts.copy()
            new[out] -= 1
            if new[out] == 0:
                del new[out]
            new[cin] += 1
            if not deck_ok(new, cards, lcol):
                continue
            s_new = evaluate(new, cards, orders, refund)
            if s_new > cur:
                counts, cur = new, s_new
        print(f"    couleur secondaire {sc or 'aucune'} : score {cur:.2f}")
        if best_global is None or cur > best_global[0]:
            best_global = (cur, counts, sc)
    return best_global


def active_leader_name(page):
    """Nom (en minuscules) du Leader du deck actif, lu dans le Deck Lab."""
    try:
        page.goto(URL + "#decks")
        time.sleep(4)
        body = page.inner_text("body")
        m = re.search(r"\n([^\n]+)\n\s*Active deck", body, re.I)
        return m.group(1).strip().lower() if m else None
    except Exception:
        return None


def build_scores(cards):
    scores = {}
    for art, c in cards.items():
        safe = art in SAFE_PROTOCOLS
        scores[art] = {
            "name": c["name"], "kind": c["kind"], "ram": c["ram"], "power": c["power"],
            "score": round(card_value(c) if c["kind"] == "C" else (0.5 if safe else 0.0), 2),
            "safe_protocol": safe,
        }
    return scores


def format_deck(cards, leader, counts, score, sec):
    lines = [f"Leader : {leader['name']} (#{leader['art']})",
             f"Score simulé : {score:.2f}   couleur secondaire : {sec or 'aucune'}", ""]
    for kind, title in (("C", "Personnages"), ("P", "Protocoles")):
        lines.append(f"{title} :")
        for art, n in sorted(counts.items(), key=lambda x: (cards[x[0]]["ram"], x[0])):
            c = cards[art]
            if c["kind"] == kind:
                tag = ""
                if kind == "P":
                    if is_support(c):
                        tag += f" [soutien +{c['power']}]"
                    if art in SAFE_PROTOCOLS:
                        tag += " [le bot le joue pour piocher]"
                lines.append(f"  {n}x #{art} {c['name']} ({c['color']}) - {c['ram']} RAM, {c['power']} Power{tag}")
        lines.append("")
    return "\n".join(lines)


def optimize_and_save(page):
    cards = parse_cards(load_db())
    leaders = [c for c in cards.values() if c["kind"] == "L"]
    if not leaders:
        print("Aucun Leader trouvé dans la base : optimisation impossible.")
        return
    active = active_leader_name(page)
    print(f"Optimisation du deck (Leader actif : {active or 'inconnu'})...")
    leaders.sort(key=lambda c: c["name"].lower() != active)   # Leader actif en premier

    best = None
    for L in leaders:
        print(f"  Leader {L['name']} ({L['color']}) :")
        res = optimize(cards, L["art"], ITERS, SIMS)
        if res is None:
            print("    pas assez de cartes pour un deck valide.")
            continue
        if best is None or res[0] > best[1][0]:
            best = (L, res)
        if L["name"].lower() == active:
            best = (L, res)      # on respecte le Leader du deck actif s'il est faisable
            break
    if best is None:
        print("Aucun deck de 30 cartes possible avec ta collection.")
        return

    leader, (score, counts, sec) = best
    txt = format_deck(cards, leader, counts, score, sec)
    print("\n" + txt)
    with open(DECK_FILE, "w", encoding="utf-8") as f:
        f.write(txt)
    with open(SCORES_PATH, "w", encoding="utf-8") as f:
        json.dump(build_scores(cards), f, ensure_ascii=False, indent=1)

    # État : deck recommandé et dernier deck réellement appliqué dans le jeu
    st = load_state()
    applied = st.get("applied")
    cur = dict(counts)
    st["leader"] = leader["art"]
    st["deck"] = cur
    save_state(st)
    print(f"Écrit : {DECK_FILE} et {SCORES_PATH}")

    if applied is None:
        print("Aucun deck appliqué enregistré : le deck recommandé sera construit.")
    else:
        diff = []
        for art in sorted(set(applied) | set(cur)):
            d = cur.get(art, 0) - applied.get(art, 0)
            if d:
                diff.append(f"  {'+' if d > 0 else '-'}{abs(d)}x #{art} {cards[art]['name']}")
        if diff:
            print("Changements par rapport au dernier deck appliqué :")
            print("\n".join(diff))
        else:
            print("Le deck recommandé est identique au dernier deck appliqué.")


# =====================================================================
# 2b. CONSTRUCTION AUTOMATIQUE DU DECK DANS LE DECK LAB
# =====================================================================
def load_state():
    try:
        with open(DECK_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(DECK_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)


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


def choose_leader(page, leader_name):
    """Sélectionne le Leader dans l'éditeur de deck. Retourne True si c'est fait."""
    want = leader_name.lower()
    editor = page.locator("div.lib-deck-editor")
    try:
        # 1) liste déroulante
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


def build_deck_in_lab(page, cards, leader, counts):
    """Crée un nouveau deck « Auto MMJJ-HHMM » avec la liste recommandée. Retourne son nom, ou None."""
    total = sum(counts.values())
    if total != DECK_SIZE:
        print(f"Deck recommandé invalide ({total} cartes au lieu de {DECK_SIZE}) : abandon.")
        return None
    name = time.strftime("Auto %m%d-%H%M")
    print(f"Création du deck « {name} » dans le Deck Lab ({leader['name']}, {total} cartes)...")
    try:
        page.goto(URL)
        time.sleep(3)
        lab = page.locator("button.quick-decks").first
        if clickable(lab):
            lab.click()
        else:
            page.goto(URL + "#decks")
        new_btn = page.locator("button.lib-primary").filter(
            has_text=re.compile(r"^\s*new deck\s*$", re.I)).first
        new_btn.wait_for(state="visible", timeout=10000)
        new_btn.click()
        page.locator("aside.lib-deck-sheet").first.wait_for(state="visible", timeout=8000)
        time.sleep(1)

        # Leader
        if not choose_leader(page, leader["name"]):
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

        # Cartes
        problems = []
        order = sorted(counts.items(), key=lambda x: (cards[x[0]]["kind"], cards[x[0]]["ram"], x[0]))
        for art, n in order:
            cname = cards[art]["name"]
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
        time.sleep(2.5)
        if name.lower() in page.inner_text("body").lower() and not clickable(save):
            print(f"  Deck « {name} » enregistré.")
            return name
        if not clickable(save):
            print(f"  Deck « {name} » probablement enregistré (non confirmé à l'écran).")
            return name
        print("  La sauvegarde ne semble pas avoir abouti.")
        dump_deck_debug(page)
        return None
    except Exception as e:
        print("  Construction du deck impossible :", str(e).splitlines()[0])
        dump_deck_debug(page)
        return None


def select_battle_deck(page, deck_name):
    """Choisit le deck dans la liste « Battle deck » de l'Arena. Retourne True si trouvé."""
    want = deck_name.lower()
    try:
        sels = page.locator("select")
        for i in range(sels.count()):
            s = sels.nth(i)
            opts = [o.strip() for o in s.locator("option").all_inner_texts()]
            for idx, o in enumerate(opts):
                if want in o.lower():
                    s.select_option(index=idx)
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
        load_scores()

    new_name = build_deck_in_lab(page, cards, leader, deck) if BUILD_DECK else None
    if new_name:
        st["applied"] = deck
        st["applied_name"] = new_name
        save_state(st)
        ACTIVE_DECK_NAME = new_name
        return
    if PAUSE_ON_NEW_DECK and sys.stdin.isatty():
        input(f"\nMets ton deck à jour à la main avec {DECK_FILE}, puis appuie sur Entrée... ")
        st["applied"] = deck
        st["applied_name"] = None
        save_state(st)


# =====================================================================
# 3. BOUCLE DE PARTIES
# =====================================================================
def play_loop(ctx):
    page = latest_page(ctx)
    if "aimasterstcg.com" not in page.url:
        page.goto(URL)
        pause(2, 3)

    games = 0
    last_turn = 0
    first_turn = None
    deck_selected = False
    was_in_game = False
    reported = False
    ts = None
    last_action = time.time()
    pool_size = load_mirror_pool()
    tlog(f"Croyance : deck adverse supposé = miroir de mon deck ({pool_size} cartes)" if pool_size
         else "Croyance indisponible (deck_state.json sans deck) : marge fixe.")

    while True:
        try:
            page = latest_page(ctx)

            # ----- En partie -----
            if in_game(page):
                was_in_game = True
                resolve = btn(page, r"resolve defense")
                end_turn = btn(page, r"end turn")
                skip = btn(page, r"^\s*skip")

                if handle_scan(page):
                    pass
                elif clickable(skip):
                    tlog("Choix optionnel ignoré :", skip.inner_text().strip())
                    skip.click()
                elif clickable(resolve):
                    defend(page, resolve)
                elif clickable(end_turn):
                    n = turn_number(page)
                    if ts is None or (n is not None and ts.turn != n):
                        ts = TurnState(n)
                        last_turn = n or last_turn
                        if first_turn is None and n:
                            first_turn = n
                        tlog(f"--- Tour {n} ---")
                    if my_turn(page, ts):
                        last_action = time.time()
                        reported = False
                        continue
                    end_turn.click()
                    tlog("End turn")
                else:
                    idle = time.time() - last_action
                    if idle > 25:
                        end = page.locator("button, a").filter(has_text=END_RE).first
                        if clickable(end):
                            print("Bouton de fin détecté :", end.inner_text().strip())
                            end.click()
                        elif not reported:
                            texts = [t.strip() for t in
                                     page.locator("button, a").all_inner_texts() if t.strip()]
                            msg = f"En attente depuis 25 s. Boutons visibles : {texts[:15]} | texte : {snippet(page, 300)}"
                            print(msg)
                            tlog(msg)
                            page.screenshot(path="waiting.png")
                            reported = True
                    if idle > STUCK_TIMEOUT:
                        page.screenshot(path="stuck.png")
                        print("Bloqué. Capture : stuck.png. Texte :", snippet(page))
                        return
                    time.sleep(1)
                    continue

                last_action = time.time()
                reported = False
                pause()
                continue

            # ----- On vient de quitter l'écran de partie -----
            if was_in_game:
                pause(2, 3)              # laisse l'écran de résultat s'afficher
                result = record_result(page)
                seat = {1: "first", 2: "second"}.get(first_turn, "?")
                tlog(f"=== Résultat : {result} au tour {last_turn} ({seat} joueur) ===")
                write_stats(games, result, last_turn, seat)
                was_in_game = False
                ts = None
                last_action = time.time()
                continue

            # ----- Lobby : Arena -> Practice -----
            start = find_start(page)
            if clickable(start):
                if ACTIVE_DECK_NAME and not deck_selected:
                    deck_selected = True
                    if not select_battle_deck(page, ACTIVE_DECK_NAME):
                        print(f"Deck « {ACTIVE_DECK_NAME} » absent de la liste Battle deck : "
                              "sélectionne-le à la main.")
                    pause(0.5, 1)
                    os.system("cls")
                if games >= MAX_GAMES:
                    print(f"{MAX_GAMES} parties faites. Fin.")
                    return
                start.click()
                games += 1
                reset_stats()
                last_turn = 0
                first_turn = None
                tlog(f"=== Partie {games} lancée (deck : {ACTIVE_DECK_NAME or 'actuel'}, "
                     f"réserve={USE_RESERVE}, guard-home={GUARD_HOME_HP}, marge={'auto' if MARGIN_AUTO else ATTACK_MARGIN}) ===")
                last_action = time.time()
                reported = False
                pause(3, 5)
                continue

            if go_to_arena(page):
                last_action = time.time()
                pause(1.5, 3)
                continue

            if time.time() - last_action > STUCK_TIMEOUT:
                page.screenshot(path="stuck.png")
                print("Bloqué hors partie. Capture : stuck.png. Texte :", snippet(page))
                return
            time.sleep(1)

        except KeyboardInterrupt:
            print("Arrêt demandé.")
            return
        except Exception as e:
            print("Erreur passagère :", str(e).splitlines()[0])
            time.sleep(1)


# =====================================================================
# POINT D'ENTRÉE
# =====================================================================
def main():
    global MAX_GAMES, PAUSE_ON_NEW_DECK, BUILD_DECK, USE_RESERVE, GUARD_HOME_HP, ATTACK_MARGIN
    global MARGIN_AUTO, PAUSE_SCALE, DEFENSE_WEIGHT, ACTIVE_DECK_NAME
    ap = argparse.ArgumentParser(description="AI Masters TCG : base de cartes + deck + parties")
    ap.add_argument("--games", type=int, default=MAX_GAMES, help="nombre de parties d'entraînement")
    ap.add_argument("--no-update", action="store_true", help="saute la mise à jour de la base de cartes")
    ap.add_argument("--force-update", action="store_true", help="rouvre toutes les cartes")
    ap.add_argument("--force-optimize", action="store_true", help="relance l'optimisation du deck")
    ap.add_argument("--no-pause", action="store_true", help="ne s'arrête pas si le deck est à faire à la main")
    ap.add_argument("--no-build", action="store_true", help="ne construit pas le deck dans le Deck Lab")
    ap.add_argument("--build-deck", action="store_true", help="reconstruit le deck recommandé même s'il est déjà appliqué")
    ap.add_argument("--reserve", action="store_true", help="TEST : garde RAM + Protocole de soutien pour la défense")
    ap.add_argument("--no-reserve", action="store_true", help="(réglage par défaut) pas de réserve de défense")
    ap.add_argument("--guard-home", type=int, default=GUARD_HOME_HP, help="TEST : PV en dessous desquels les Guards n'attaquent plus (0 = jamais)")
    ap.add_argument("--margin", default="0", help="marge de puissance au-dessus de la défense du Leader : "
                    "un entier fixe (0 par défaut) ou 'auto' (croyance sur la main adverse, à tester)")
    ap.add_argument("--defense-weight", type=float, default=DEFENSE_WEIGHT,
                    help="TEST : valeur d'un coup bloqué dans l'optimisation du deck (0 = deck offensif)")
    ap.add_argument("--deck", default=None, help="impose le deck de l'Arena (nom ou début du nom) sans rien construire")
    ap.add_argument("--speed", type=float, default=PAUSE_SCALE,
                    help="multiplicateur des pauses (0.6 = plus rapide ; à tester avant de s'y fier)")
    ap.add_argument("--report", action="store_true", help="affiche le bilan de stats.csv et quitte")
    args = ap.parse_args()
    if args.report:
        report_stats()
        return
    GUARD_HOME_HP = args.guard_home
    if str(args.margin).lower() == "auto":
        MARGIN_AUTO = True
    else:
        try:
            ATTACK_MARGIN = int(args.margin)
        except ValueError:
            raise SystemExit("--margin attend 'auto' ou un entier")
        MARGIN_AUTO = False
    PAUSE_SCALE = max(0.2, args.speed)
    USE_RESERVE = bool(args.reserve) and not args.no_reserve
    DEFENSE_WEIGHT = args.defense_weight
    MAX_GAMES = args.games
    if args.no_pause:
        PAUSE_ON_NEW_DECK = False
    if args.no_build:
        BUILD_DECK = False

    ensure_chrome()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        page = latest_page(ctx)

        # 1) base de cartes
        changed = False
        if not args.no_update:
            try:
                changed = update_db(page, force=args.force_update)
            except Exception as e:
                print("Mise à jour de la base impossible :", str(e).splitlines()[0])

        # 2) deck (seulement si la collection a changé, ou si on ne l'a jamais calculé)
        need_opt = changed or args.force_optimize or not os.path.exists(SCORES_PATH)
        if need_opt and os.path.exists(DB_PATH):
            try:
                optimize_and_save(page)
            except Exception as e:
                print("Optimisation impossible :", str(e).splitlines()[0])
        else:
            print("Collection inchangée : deck recommandé conservé.")
        load_scores()

        # 2b) construire le deck recommandé dans le Deck Lab s'il n'est pas encore appliqué
        if args.deck:
            ACTIVE_DECK_NAME = args.deck
            print(f"Deck imposé : « {args.deck} » (aucune construction automatique).")
        else:
            try:
                maybe_apply_deck(page, force=args.build_deck)
            except Exception as e:
                print("Application du deck impossible :", str(e).splitlines()[0])

        # 3) parties
        page.goto(URL)
        pause(2, 3)
        play_loop(ctx)


if __name__ == "__main__":
    main()
    
"""
python test.py --deck "AUTO 1102-1411" --games 50
"""