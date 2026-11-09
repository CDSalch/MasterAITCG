"""
deck_builder.py - Deck optimization algorithm (Monte Carlo) and Deck Lab automation.
"""
import json
import random
import re
import time
from collections import Counter
import config
from browser_driver import clickable
from database import load_db


def load_state():
    """Load persistent deck state."""
    try:
        with open(config.DECK_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    """Save persistent deck state."""
    with open(config.DECK_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)


def parse_cards(entries):
    """Parse raw database entries into structured card objects."""
    cards = {}
    for c in entries:
        front = c.get("front")
        if not front:
            continue
        t = front["text"]
        art = c["art"]
        if "RESOURCE VALUE" in t:
            continue
        name_m = re.search(r"Inspect (.+), (?:[^,]+ · )?[A-Za-z ]+$", c["label"])
        name = name_m.group(1) if name_m else c["label"]
        color = re.search(r"COLOR\n(\w+)", t)
        power = re.search(r"POWER\n(\d+)", t)
        ram = re.search(r"RAM\n(\d+)", t)
        hp = re.search(r"HP\n(\d+)", t)
        owned = re.search(r"×(\d+)", c["tile"])
        ability = t.split("ABILITY")[1:] if "ABILITY" in t else []
        text = " ".join(" ".join(a.split()) for a in ability).split("Share on X")[0]
        num_m = re.match(r"\d+", art)
        n = int(num_m.group(0)) if num_m else 0

        if hp is not None and ram is None:
            kind = "L"
        elif any(a <= n <= b for a, b in config.PROTOCOL_RANGES):
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
    """Compute base deployment value for a Character card."""
    return ch["power"] + (1.0 if ch["guard"] else 0.0) + (1.5 if ch["rush"] else 0.0)


def best_deploy(hand_chars, capacity, slots, leader_refund):
    """Select optimal character deployment combination via knapsack dynamic programming."""
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
    """Check if card can serve as defense support Protocol."""
    return c["kind"] == "P" and c["power"] >= 1


def simulate(deck, orders, leader_refund):
    """Simulate Monte Carlo game draws to evaluate deck performance score."""
    defensive = config.DEFENSE_WEIGHT > 0
    total = 0.0
    for s, order in enumerate(orders):
        library = [deck[i] for i in order]
        hand = library[:5]
        pos = 5
        second = (s % 2 == 1)
        board = []
        score = 0.0

        for t in range(1, config.SIM_TURNS + 1):
            if t > 1 or second:
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
                    if (c["kind"] == "P" and c["art"] in config.SAFE_PROTOCOLS and c["ram"] <= cap
                            and pos < len(library)
                            and (not defensive or not is_support(c) or sum(1 for h in hand if is_support(h)) >= 2)):
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
                if defensive and sup:
                    hand.remove(min(sup, key=lambda c: c["ram"]))
                    score += config.DEFENSE_WEIGHT
        total += score
    return total / len(orders)


def collector_no(art):
    """Collector number shared by every print of a card (037 and 037-P -> 37)."""
    m = re.match(r"\d+", art)
    return int(m.group(0)) if m else -1


def number_limit(num):
    """Rulebook: max copies per collector number, all prints together; only 1 for #039."""
    return 1 if num == 39 else config.MAX_COPIES


def deck_ok(counts, cards, leader_color):
    """Validate deck composition constraints (30 cards, 1 secondary color max, max 10 secondary)."""
    if sum(counts.values()) != config.DECK_SIZE:
        return False
    per_num = Counter()
    for art, n in counts.items():
        per_num[collector_no(art)] += n
    if any(n > number_limit(num) for num, n in per_num.items()):
        return False
    other = Counter()
    for art, n in counts.items():
        col = cards[art]["color"]
        if col != leader_color:
            other[col] += n
    return len(other) <= 1 and all(n <= config.SECONDARY_MAX for n in other.values())


def copy_limit(card):
    """Return maximum playable copies based on ownership."""
    return min(config.MAX_COPIES, card["owned"])


def static_score(c):
    """Compute heuristic initial priority score for deck building."""
    if c["kind"] == "C":
        return c["power"] / max(c["ram"], 1) ** 0.5 + (1 if c["guard"] else 0) + (1.5 if c["rush"] else 0)
    base = 2.0 if c["art"] in config.SAFE_PROTOCOLS else 0.3
    if config.DEFENSE_WEIGHT > 0 and is_support(c):
        base += 1.5 if c["ram"] == 0 else 0.7
    return base


def initial_deck(cards, leader_color, second_color):
    """Build greedy starting deck based on static scores."""
    pool = [c for c in cards.values() if c["kind"] in "CP" and c["color"] in (leader_color, second_color)]
    pool.sort(key=static_score, reverse=True)
    counts = Counter()
    other = 0
    per_num = Counter()
    for c in pool:
        num = collector_no(c["art"])
        for _ in range(copy_limit(c)):
            if sum(counts.values()) >= config.DECK_SIZE:
                break
            if per_num[num] >= number_limit(num):
                break
            if c["color"] != leader_color:
                if other >= config.SECONDARY_MAX:
                    break
                other += 1
            per_num[num] += 1
            counts[c["art"]] += 1
    return counts


def evaluate(counts, cards, orders, refund):
    """Evaluate deck score over pre-generated random library orders."""
    deck = []
    for art, n in counts.items():
        deck += [cards[art]] * n
    return simulate(deck, orders, refund)


def make_orders(sims, seed=7):
    """Generate fixed random card order draws for consistent evaluation."""
    rnd = random.Random(seed)
    orders = []
    for _ in range(sims):
        o = list(range(config.DECK_SIZE))
        rnd.shuffle(o)
        orders.append(o)
    return orders


def optimize(cards, leader_art, iters, sims, seed=7):
    """Optimize deck composition using stochastic local search."""
    rnd = random.Random(seed)
    leader = cards[leader_art]
    lcol = leader["color"]
    refund = (leader["name"] == "Jensen Huang")
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

        print(f"    Secondary color {sc or 'None'}: simulated score {cur:.2f}")
        if best_global is None or cur > best_global[0]:
            best_global = (cur, counts, sc)
    return best_global


def build_scores(cards):
    """Generate card priority JSON mapping for in-game deployment decisions."""
    scores = {}
    for art, c in cards.items():
        safe = art in config.SAFE_PROTOCOLS
        scores[art] = {
            "name": c["name"], "kind": c["kind"], "ram": c["ram"], "power": c["power"],
            "score": round(card_value(c) if c["kind"] == "C" else (0.5 if safe else 0.0), 2),
            "safe_protocol": safe,
        }
    return scores


def format_deck(cards, leader, counts, score, sec):
    """Format human-readable deck layout text output."""
    lines = [f"Leader: {leader['name']} (#{leader['art']})",
             f"Simulated Score: {score:.2f} | Secondary Color: {sec or 'None'}", ""]
    for kind, title in (("C", "Characters"), ("P", "Protocols")):
        lines.append(f"{title}:")
        for art, n in sorted(counts.items(), key=lambda x: (cards[x[0]]["ram"], x[0])):
            c = cards[art]
            if c["kind"] == kind:
                tag = ""
                if kind == "P":
                    if is_support(c):
                        tag += f" [Support +{c['power']}]"
                    if art in config.SAFE_PROTOCOLS:
                        tag += " [Auto-play Draw Protocol]"
                lines.append(f"  {n}x #{art} {c['name']} ({c['color']}) - {c['ram']} RAM, {c['power']} Power{tag}")
        lines.append("")
    return "\n".join(lines)


def active_leader_name(page):
    """Nom (en minuscules) du Leader du deck actif, lu dans le Deck Lab."""
    try:
        page.goto(config.URL + "#decks")
        time.sleep(4)
        body = page.inner_text("body")
        m = re.search(r"\n([^\n]+)\n\s*Active deck", body, re.I)
        return m.group(1).strip().lower() if m else None
    except Exception:
        return None


def optimize_and_save(page):
    """Optimise le deck (en gardant le Leader du deck actif s'il est faisable), puis écrit
    deck_recommande.txt, card_scores.json et deck_state.json (leader + deck recommandés)."""
    cards = parse_cards(load_db())
    leaders, seen = [], set()
    for c in sorted((c for c in cards.values() if c["kind"] == "L"),
                    key=lambda c: (c["name"], "-" in c["art"], c["art"])):
        if c["name"] not in seen:       # une seule version par Leader (l'art alternatif est identique)
            seen.add(c["name"])
            leaders.append(c)
    if not leaders:
        print("Aucun Leader trouvé dans la base : optimisation impossible.")
        return
    active = active_leader_name(page)
    print(f"Optimisation du deck (Leader actif : {active or 'inconnu'})...")
    leaders.sort(key=lambda c: c["name"].lower() != active)   # Leader actif en premier

    best = None
    for L in leaders:
        print(f"  Leader {L['name']} ({L['color']}) :")
        res = optimize(cards, L["art"], config.ITERS, config.SIMS)
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
    with open(config.DECK_FILE, "w", encoding="utf-8") as f:
        f.write(txt)
    with open(config.SCORES_PATH, "w", encoding="utf-8") as f:
        json.dump(build_scores(cards), f, ensure_ascii=False, indent=1)

    # État : deck recommandé et dernier deck réellement appliqué dans le jeu
    st = load_state()
    applied = st.get("applied")
    cur = dict(counts)
    st["leader"] = leader["art"]
    st["deck"] = cur
    save_state(st)
    print(f"Écrit : {config.DECK_FILE} et {config.SCORES_PATH}")

    if applied is None:
        print("Aucun deck appliqué enregistré : le deck recommandé sera construit.")
    else:
        diff = []
        for art in sorted(set(applied) | set(cur)):
            d = cur.get(art, 0) - applied.get(art, 0)
            if d:
                diff.append(f"  {'+' if d > 0 else '-'}{abs(d)}x #{art} {cards[art]['name'] if art in cards else '?'}")
        if diff:
            print("Changements par rapport au dernier deck appliqué :")
            print("\n".join(diff))
        else:
            print("Le deck recommandé est identique au dernier deck appliqué.")