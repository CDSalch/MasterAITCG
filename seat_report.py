"""
seat_report.py - Analyse de game_log.txt (et game_log_old.txt) par position (premier / second joueur).

Répond à : où se perdent les parties du premier joueur ?
  - la défense visée par les attaques au Leader selon le tour (le bonus +1 des tours 3, 5, 7 est-il compté deux fois ?),
  - le taux de coups touchés par tour,
  - la RAM dépensée en attache vs en déploiement,
  - l'écart de PV (moi - adversaire) tour par tour, victoires vs défaites.

Les parties sont séparées en deux séries : AVANT le correctif du bonus (+1 compté deux fois) et APRÈS
(lignes « Partie n lancée » contenant « nobonus »), pour ne pas mélanger les deux comportements.

Utilisation :  python seat_report.py            (cherche le journal défini dans config.LOG_PATH)
               python seat_report.py --new      (uniquement la série APRÈS correctif)
               python seat_report.py journal.txt [autre.txt ...]
"""
import math
import re
import sys
from collections import Counter, defaultdict

try:
    import config
    DEFAULT = [config.LOG_PATH.replace(".txt", "_old.txt"), config.LOG_PATH]
except Exception:
    DEFAULT = ["game_log_old.txt", "game_log.txt"]

PFX = re.compile(r"^\d\d:\d\d:\d\d ")
R_START = re.compile(r"=== Partie (\d+) lancée")
R_TURN = re.compile(r"--- Tour (\d+) ---")
R_ATT = re.compile(r"ATTACK : (.+?) \((\d+)\) -> Leader adverse \(défense (\d+)\)(?: -> (touché|bloqué))?")
R_ATTACH = re.compile(r"RAM attachée à (.+?) : (\d+)")
R_RES = re.compile(r"=== Résultat : (win|loss|\?) au tour (\d+) \((first|second|\?) joueur\) ===")
R_HP = re.compile(r"PV : moi (\d+) / adversaire (\d+) \| il faut (\d+) Power")
R_DEPLOY = re.compile(r"^\s{2}(?!ATTACK|RAM attachée|Défense)(\S.*?) : (.+) \((\d+) RAM\)\s*$")


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def parse(paths):
    games, cur, turn = [], None, 0
    prev_end, gaps = None, []
    for path in paths:
        try:
            f = open(path, encoding="utf-8")
        except OSError:
            continue
        with f:
            for raw in f:
                tm = re.match(r"^(\d\d):(\d\d):(\d\d) ", raw)
                sec = int(tm.group(1)) * 3600 + int(tm.group(2)) * 60 + int(tm.group(3)) if tm else None
                msg = PFX.sub("", raw.rstrip("\n"))
                if R_START.search(msg):
                    cur, turn = {"att": [], "attach": [], "deploy": [], "hp": [], "t0": sec,
                                 "regime": "apres" if "nobonus" in msg else "avant"}, 0
                    if prev_end is not None and sec is not None:
                        gap = (sec - prev_end) % 86400
                        if gap < 600:
                            gaps.append(gap)          # temps passé hors partie (lobby, écran de résultat)
                    continue
                if cur is None:
                    continue
                m = R_TURN.search(msg)
                if m:
                    turn = int(m.group(1))
                    continue
                m = R_ATT.search(msg)
                if m:
                    cur["att"].append((turn, int(m.group(2)), int(m.group(3)), m.group(4)))
                    continue
                m = R_ATTACH.search(msg)
                if m:
                    cur["attach"].append((turn, int(m.group(2))))
                    continue
                m = R_HP.search(msg)
                if m:
                    cur["hp"].append((turn, int(m.group(1)), int(m.group(2)), int(m.group(3))))
                    continue
                m = R_RES.search(msg)
                if m:
                    cur.update(result=m.group(1), turns=int(m.group(2)), seat=m.group(3))
                    if cur.get("t0") is not None and sec is not None:
                        cur["dur"] = (sec - cur["t0"]) % 86400
                    prev_end = sec
                    games.append(cur)
                    cur = None
                    continue
                m = R_DEPLOY.match(msg)
                if m:
                    cur["deploy"].append((turn, int(m.group(3))))
    parse.gaps = gaps
    return games


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def section(title, gs):
    wins = sum(1 for g in gs if g["result"] == "win")
    lo, hi = wilson(wins, len(gs))
    print(f"=== {title} : {wins}/{len(gs)} victoires ({100 * wins / len(gs):.0f} %, IC {100 * lo:.0f}-{100 * hi:.0f} %) ===")

    # 1) attaques au Leader par tour
    by_turn = defaultdict(list)
    for g in gs:
        for t, p, x, out in g["att"]:
            by_turn[t].append((p, x, out))
    print("  Attaques au Leader adverse par tour : n | touchés | puissance moy. | défense visée (valeur:nombre)")
    for t in sorted(by_turn):
        rows = by_turn[t]
        judged = [r for r in rows if r[2] in ("touché", "bloqué")]
        hit = sum(1 for r in judged if r[2] == "touché")
        xs = Counter(r[1] for r in rows).most_common(3)
        print(f"    tour {t:2d} : {len(rows):3d} | {100 * hit / len(judged) if judged else float('nan'):4.0f} % | "
              f"{mean(r[0] for r in rows):.1f} | " + ", ".join(f"{x}:{n}" for x, n in xs))

    # 2) RAM : attache vs déploiement
    att_ram = mean(sum(n for _, n in g["attach"]) for g in gs)
    dep_ram = mean(sum(c for _, c in g["deploy"]) for g in gs)
    dep_n = mean(len(g["deploy"]) for g in gs)
    early = mean(sum(1 for t, _ in g["deploy"] if t <= 6) for g in gs)
    print(f"  RAM par partie : {att_ram:.1f} attachées | {dep_ram:.1f} dépensées en déploiement "
          f"({dep_n:.1f} cartes, dont {early:.1f} jusqu'au tour 6)")

    # 3) écart de PV (moi - adversaire) par tour, victoires vs défaites
    diff = {"win": defaultdict(list), "loss": defaultdict(list)}
    for g in gs:
        if g["result"] in diff:
            last = {}
            for t, me, opp, _ in g["hp"]:
                last[t] = me - opp
            for t, d in last.items():
                diff[g["result"]][t].append(d)
    turns = sorted(set(diff["win"]) | set(diff["loss"]))
    if turns:
        print("  Écart de PV (moi - adversaire) par tour : victoires | défaites")
        for t in turns:
            if t > 12:
                break
            w, l = diff["win"].get(t, []), diff["loss"].get(t, [])
            print(f"    tour {t:2d} : {mean(w):+5.1f} (n={len(w):2d}) | {mean(l):+5.1f} (n={len(l):2d})")
    print()


def main(paths, only_new=False):
    games = parse(paths)
    if only_new:
        games = [g for g in games if g["regime"] == "apres"]
    if not games:
        print("Aucune partie complète trouvée dans", paths, "(le journal ne contient-il que des anciennes parties ?)")
        return
    names = {"avant": "AVANT correctif du bonus", "apres": "APRÈS correctif (nobonus)"}
    seats = (("first", "premier joueur"), ("second", "second joueur"))
    print(f"{len(games)} parties analysées\n")
    print("Résumé :")
    for reg in ("avant", "apres"):
        for seat, sname in seats:
            gs = [g for g in games if g["regime"] == reg and g["seat"] == seat]
            if gs:
                w = sum(1 for g in gs if g["result"] == "win")
                lo, hi = wilson(w, len(gs))
                print(f"  {names[reg]:28s} {sname:15s} {w:3d}/{len(gs):3d} ({100 * w / len(gs):3.0f} %, IC {100 * lo:.0f}-{100 * hi:.0f} %)")
    print()
    durs = sorted(g["dur"] for g in games if g.get("dur") is not None and 20 < g["dur"] < 3600)
    if durs:
        wins = sum(1 for g in games if g["result"] == "win")
        wr = wins / len(games)
        gaps = getattr(parse, "gaps", [])
        gap = mean(gaps) if gaps else 0.0
        cycle = mean(durs) + gap
        print(f"Cadence : partie {mean(durs) / 60:.1f} min en moyenne (médiane {durs[len(durs) // 2] / 60:.1f}), "
              f"{gap:.0f} s hors partie -> {3600 / cycle:.1f} parties/heure")
        print(f"Tokens : {10 + 40 * wr:.1f} par partie (win rate {100 * wr:.0f} %) -> environ {(10 + 40 * wr) * 3600 / cycle:.0f} tokens/heure")
        by_seat = {sd: [g["dur"] for g in games if g["seat"] == sd and g.get("dur") and 20 < g["dur"] < 3600] for sd, _ in seats}
        print("  durée par position : " + " | ".join(f"{n} {mean(by_seat[sd]) / 60:.1f} min" for sd, n in seats if by_seat[sd]))
        print()
    for reg in ("avant", "apres"):
        for seat, sname in seats:
            gs = [g for g in games if g["regime"] == reg and g["seat"] == seat]
            if gs:
                section(f"{names[reg]} - {sname.upper()}", gs)


if __name__ == "__main__":
    args = sys.argv[1:]
    only_new = "--new" in args
    paths = [a for a in args if not a.startswith("--")]
    main(paths or DEFAULT, only_new)