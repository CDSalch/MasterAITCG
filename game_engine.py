"""
game_engine.py - Logique de jeu (déploiement, attaque, RAM, défense, croyance, stats).

Port FIDÈLE de la stratégie de main_1.py (celle qui faisait ~55-60 % de victoires) dans la
structure modulaire. Les réglages de comportement sont lus dans config.py s'ils y existent,
sinon les valeurs par défaut de main_1.py (offensives) s'appliquent : voir _DEFAULTS.
"""
import csv
import json
import os
import random
import re
import time

import config
from browser_driver import (
    attr, btn, clickable, close_hand_sheet, close_overlays,
    label, pause, safe_click, tlog
)

# ---------- Sélecteurs (relevés avec recorder.py ; ceux de main_1.py, qui marchaient) ----------
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

# ---------- Réglages de comportement : config.py d'abord, sinon valeurs de main_1.py ----------
_DEFAULTS = {
    "MAX_DEPLOY_PER_TURN": 4,
    "PLAY_PROTOCOLS": False,
    "USE_RESERVE": False,        # défense : garder RAM + soutien (désactivé = offensif)
    "KEEP_SUPPORT": 1,
    "LEADER_BONUS_TURNS": (3, 5, 7),
    # Le label du Leader adverse affiche DÉJÀ sa défense effective (6 aux tours 3, 5, 7, sinon 5) :
    # seat_report.py l'a confirmé sur 110 parties en premier joueur (« défense visée » toujours 7 = 6 + 1).
    # Tant que ce réglage est True, LEADER_BONUS_TURNS est ignoré (pas de +1 en double).
    "LEADER_BONUS_IN_LABEL": True,
    "ATTACK_MARGIN": 0,
    "MARGIN_AUTO": False,
    "GUARD_HOME_HP": 0,
    "INTERCEPT_CHUMP_HP": 2,
    "MARGIN_RAM_COST": 0.12,
    "BELIEF_SAMPLES": 400,
    "STATS_PATH": "stats.csv",
}


SEAT = None   # "first" / "second", fixé par main.py dès que le 1er tour de la partie est connu


def set_seat(first_turn):
    """Mémorise la position pour la partie en cours (None = inconnue)."""
    global SEAT
    SEAT = {1: "first", 2: "second"}.get(first_turn)


class _Cfg:
    """Accès aux réglages. Pour chaque réglage X, config.X_FIRST / config.X_SECOND (s'ils existent)
    remplacent config.X quand on joue en premier / en second."""
    def __getattr__(self, name):
        if name == "LEADER_BONUS_TURNS" and self.LEADER_BONUS_IN_LABEL:
            return ()
        if SEAT:
            alt = f"{name}_{SEAT.upper()}"
            if hasattr(config, alt):
                return getattr(config, alt)
        if hasattr(config, name):
            return getattr(config, name)
        try:
            return _DEFAULTS[name]
        except KeyError:
            raise AttributeError(name)


C = _Cfg()

CARD_SCORES = {}
ACTIVE_DECK_NAME = None
GS = {}
STATS = {"games": 0, "wins": 0, "tokens": 0}
STATS_FIELDS = ["time", "game", "deck", "config", "result", "turns", "seat", "attacks", "leader_hits",
                "leader_blocked", "hits_taken", "defenses_blocked", "supports", "guards",
                "m0_att", "m0_hit", "m1_att", "m1_hit", "pred_hit"]

# Résultat de partie : regex de main_1.py (ne PAS les resserrer sans avoir lu match_report_debug.txt)
WIN_RE = re.compile(r"\b(victory|you win|you won|win)\b", re.I)
LOSS_RE = re.compile(r"\b(defeat|you lose|you lost|loss)\b", re.I)
TOKEN_RES = [
    re.compile(r"\+\s*(\d[\d.,]*)\s*(?:\$?\w+\s+)?tokens?", re.I),
    re.compile(r"tokens?\D{0,15}\+?\s*(\d[\d.,]*)", re.I),
]
REPORT_DEBUG_DONE = False
DEFENSE_DEBUG_DONE = False
MIRROR_POOL = []   # deck adverse supposé (miroir du mien en Practice)

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


def _load_state():
    try:
        with open(config.DECK_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_scores():
    """Charge card_scores.json (priorités de déploiement) dans CARD_SCORES."""
    global CARD_SCORES
    try:
        with open(config.SCORES_PATH, encoding="utf-8") as f:
            CARD_SCORES = json.load(f)
        print(f"{config.SCORES_PATH} chargé ({len(CARD_SCORES)} cartes)")
    except Exception:
        CARD_SCORES = {}
        print(f"{config.SCORES_PATH} absent : déploiement par coût décroissant.")


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


def turn_number(page):
    try:
        txt = page.locator(".cb-crossbar").first.inner_text(timeout=1000)
    except Exception:
        return None
    m = re.search(r"TURN\s*0*(\d+)", txt, re.I)
    return int(m.group(1)) if m else None


def hp_of(text):
    m = re.search(r"(\d+)\s*HP", text or "", re.I)
    return int(m.group(1)) if m else None


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


def try_deploy(page, ts, reserve=0, keep_support=True):
    """Pose un Personnage (ou joue un Protocole de pioche). Garde `reserve` RAM prête et au moins
    C.KEEP_SUPPORT Protocole de soutien en main pour défendre. Retourne True si une action a été faite."""
    if ts.played >= C.MAX_DEPLOY_PER_TURN:
        return False
    ram = ready_ram(page)
    n_support = len(hand_supports(page)) if (C.USE_RESERVE and keep_support) else 99
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
        if info.get("kind") == "P" and not info.get("safe_protocol") and not C.PLAY_PROTOCOLS:
            ts.tried.add(key)
            continue
        # on garde la RAM de soutien, et le dernier Protocole de soutien pour la défense
        if ram is not None and cost_of(lab) > ram - reserve:
            continue
        if info.get("kind") == "P" and info.get("power", 0) >= 1 and n_support <= C.KEEP_SUPPORT:
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
        if re.search(r"execute", txt, re.I) and not (C.PLAY_PROTOCOLS or safe):
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

    bonus = 1 if turn in C.LEADER_BONUS_TURNS else 0
    if leader and my_power >= leader[1] + bonus:
        return leader[0], f"Leader adverse (défense {leader[1] + bonus})"
    if best_char:
        return best_char[0], f"KO {name_of(best_char[2])} ({best_char[1]} Power)"
    return None, ""


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
    return opp_def + (1 if turn in C.LEADER_BONUS_TURNS else 0)


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


def reset_stats():
    GS.clear()
    GS.update({"attacks": 0, "leader_hits": 0, "leader_blocked": 0, "taken": 0,
               "def_blocked": 0, "supports": 0, "guards": 0,
               "m0_att": 0, "m0_hit": 0, "m1_att": 0, "m1_hit": 0, "pred_sum": 0.0, "pred_n": 0})


def config_tag():
    tag = (f"reserve={int(C.USE_RESERVE)} guardhome={C.GUARD_HOME_HP} "
           f"margin={'auto' if C.MARGIN_AUTO else C.ATTACK_MARGIN} keep={C.KEEP_SUPPORT}")
    if not C.LEADER_BONUS_TURNS:      # suffixe seulement hors réglage par défaut : l'historique reste groupé
        tag += " nobonus"
    return tag


def rotate_old_stats():
    """Si stats.csv a l'ancien format de colonnes, on le range à part pour ne pas mélanger les lignes."""
    try:
        with open(C.STATS_PATH, encoding="utf-8") as f:
            header = f.readline().strip()
    except Exception:
        return
    if header != ",".join(STATS_FIELDS):
        try:
            os.replace(C.STATS_PATH, C.STATS_PATH.replace(".csv", time.strftime("_ancien_%m%d-%H%M.csv")))
        except Exception:
            pass


def write_stats(game, result, turns, seat="?"):
    """Ajoute une ligne à stats.csv et affiche le bilan de la partie."""
    rotate_old_stats()
    new = not os.path.exists(C.STATS_PATH)
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
        with open(C.STATS_PATH, "a", newline="", encoding="utf-8") as f:
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
    if not C.USE_RESERVE:
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


def load_mirror_pool():
    """En « Practice », l'adversaire joue un miroir de mon deck (texte de l'écran Arena) : sa liste
    est donc connue. On prend le dernier deck appliqué (sinon le deck recommandé). Retourne la taille."""
    global MIRROR_POOL
    st = _load_state()
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


def block_probabilities(pool, hand_size, opp_ram, n=C.BELIEF_SAMPLES):
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
    """Marge m qui maximise P(coup passe) - C.MARGIN_RAM_COST * m (chaque point de marge coûte 1 RAM)."""
    if not probs:
        return C.ATTACK_MARGIN
    best_m, best_v = 0, probs[0]
    for m in range(1, 4):
        v = probs[m] - C.MARGIN_RAM_COST * m
        if v > best_v + 1e-9:
            best_m, best_v = m, v
    return best_m


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
        elif hp is not None and hp <= C.INTERCEPT_CHUMP_HP:
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
        ts.margin = choose_margin(ts.belief) if (C.MARGIN_AUTO and ts.belief) else C.ATTACK_MARGIN

    allin = lethal_possible([(f, p) for f, p, _ in cand_all], need, ram, opp_hp)
    reserve = 0 if allin else support_reserve(page)
    budget = None if ram is None else max(0, ram - reserve)

    cand = []
    for fid, pw, lab in cand_all:
        if (fid != "leader" and "guard" in lab.lower() and not allin
                and my_hp is not None and my_hp <= C.GUARD_HOME_HP):
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


reset_stats()