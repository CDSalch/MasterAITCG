"""
main.py - Point d'entrée et boucle de parties pour AI Masters TCG Bot.

La boucle de parties reprend celle de main_1.py (invite « Private Scan », choix optionnels
« skip », détection des boutons de fin, remise à zéro des stats à chaque partie).
"""
import argparse
import os
import re
import sys
import time
from playwright.sync_api import sync_playwright

import config
from browser_driver import (
    btn, clickable, ensure_chrome, latest_page, pause, snippet, tlog
)
import database
import deck_builder
import deck_lab
import game_engine as ge

START_RE = r"^\s*(practice|start training)\s*$"
END_RE = re.compile(
    r"^\s*(continue|ok|claim|back to arena|return to arena|play again|keep this order|confirm|done)\s*$", re.I
)


def go_to_arena(page):
    """Depuis un écran hors partie (résultat, accueil...), va vers le lobby de l'Arena.
    Retourne True si un clic a été fait."""
    candidates = [
        btn(page, r"back to arena"),                        # écran de résultat
        page.locator("button.quick-arena").first,           # « DUEL ARENA » sur l'accueil
        page.get_by_role("link", name=re.compile(r"^\s*arena\s*$", re.I)).first,   # menu du haut
        btn(page, r"^\s*hub\s*$"),                          # ancien bouton de retour
        page.locator("a.brand").first,                      # logo : retour à l'accueil
    ]
    for loc in candidates:
        if clickable(loc):
            try:
                loc.click(timeout=4000)
                return True
            except Exception:
                continue
    return False


def play_loop(ctx):
    """Boucle de parties : Arena -> Practice -> jouer -> résultat -> ..."""
    page = latest_page(ctx)
    if "aimasterstcg.com" not in page.url:
        page.goto(config.URL)
        pause(2, 3)

    games = 0
    last_turn = 0
    first_turn = None
    was_in_game = False
    reported = False
    deck_selected = False
    ts = None
    last_action = time.time()
    pool_size = ge.load_mirror_pool()
    tlog(f"Croyance : deck adverse supposé = miroir de mon deck ({pool_size} cartes)" if pool_size
         else "Croyance indisponible (deck_state.json sans deck) : marge fixe.")

    while True:
        try:
            page = latest_page(ctx)

            # ----- En partie -----
            if ge.in_game(page):
                was_in_game = True
                resolve = btn(page, r"resolve defense")
                end_turn = btn(page, r"end turn")
                skip = btn(page, r"^\s*skip")

                if ge.handle_scan(page):
                    pass
                elif clickable(skip):
                    tlog("Choix optionnel ignoré :", skip.inner_text().strip())
                    skip.click()
                elif clickable(resolve):
                    ge.defend(page, resolve)
                elif clickable(end_turn):
                    n = ge.turn_number(page)
                    if ts is None or (n is not None and ts.turn != n):
                        ts = ge.TurnState(n)
                        last_turn = n or last_turn
                        if first_turn is None and n:
                            first_turn = n
                            ge.set_seat(first_turn)
                            tlog(f"Position : {'premier' if first_turn == 1 else 'second'} joueur ({ge.config_tag()})")
                        tlog(f"--- Tour {n} ---")
                    if ge.my_turn(page, ts):
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
                    if idle > config.STUCK_TIMEOUT:
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
                result = ge.record_result(page)
                seat = {1: "first", 2: "second"}.get(first_turn, "?")
                tlog(f"=== Résultat : {result} au tour {last_turn} ({seat} joueur) ===")
                ge.write_stats(games, result, last_turn, seat)
                was_in_game = False
                ts = None
                last_action = time.time()
                continue

            # ----- Lobby : Arena -> Practice -----
            start = btn(page, START_RE)
            if clickable(start):
                if ge.ACTIVE_DECK_NAME and not deck_selected:
                    deck_selected = True
                    if not deck_lab.select_battle_deck(page, ge.ACTIVE_DECK_NAME):
                        print(f"Deck « {ge.ACTIVE_DECK_NAME} » absent de la liste Battle deck : "
                              "les parties se jouent avec le Battle deck actuel (stats étiquetées « ? »).")
                        ge.ACTIVE_DECK_NAME = None
                    pause(0.5, 1)
                if games >= config.MAX_GAMES:
                    print(f"\n{config.MAX_GAMES} parties terminées. Fin de la session.")
                    return
                start.click()
                games += 1
                ge.reset_stats()
                ge.set_seat(None)
                last_turn = 0
                first_turn = None
                tlog(f"=== Partie {games} lancée (deck : {ge.ACTIVE_DECK_NAME or 'actuel'}, {ge.config_tag()}) ===")
                last_action = time.time()
                reported = False
                pause(3, 5)
                continue

            if go_to_arena(page):
                last_action = time.time()
                pause(1.5, 3)
                continue

            if time.time() - last_action > config.STUCK_TIMEOUT:
                page.screenshot(path="stuck.png")
                print("Bloqué hors partie. Capture : stuck.png. Texte :", snippet(page))
                return
            time.sleep(1)

        except KeyboardInterrupt:
            print("Arrêt demandé par l'utilisateur.")
            return
        except Exception as e:
            print("Erreur passagère :", str(e).splitlines()[0])
            time.sleep(1)


def main():
    """Pipeline de démarrage : base de cartes -> deck recommandé -> deck construit et sélectionné -> parties.

    Options propres à ce fichier (en plus de celles de config.parse_args) :
      --deck NOM     impose le deck de l'Arena (nom ou début du nom), sans rien construire
      --build-deck   reconstruit le deck recommandé même s'il est déjà appliqué
      --no-build     ne construit pas le deck dans le Deck Lab (garde le Battle deck actuel)
      --no-pause     ne s'arrête pas si le deck doit être monté à la main
    """
    extra = argparse.ArgumentParser(add_help=False)
    extra.add_argument("--deck", default=None)
    extra.add_argument("--build-deck", action="store_true")
    extra.add_argument("--no-build", action="store_true")
    extra.add_argument("--no-pause", action="store_true")
    ours, rest = extra.parse_known_args()
    sys.argv = [sys.argv[0]] + rest          # config.parse_args ne connaît pas nos options
    args = config.parse_args()

    deck_lab.BUILD_DECK = getattr(config, "BUILD_DECK", True) and not ours.no_build
    deck_lab.PAUSE_ON_NEW_DECK = getattr(config, "PAUSE_ON_NEW_DECK", True) and not ours.no_pause

    ensure_chrome()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(config.CDP)
        ctx = browser.contexts[0]
        page = latest_page(ctx)

        # Étape 1 : base de cartes
        changed = False
        if not args.no_update:
            try:
                changed = database.update_db(page, force=args.force_update)
            except Exception as e:
                print("Mise à jour de la base impossible :", str(e).splitlines()[0])

        # Étape 2 : deck recommandé (si la collection a changé, s'il n'a jamais été appliqué, ou sur demande)
        never_applied = not deck_builder.load_state().get("applied")
        need_opt = changed or args.force_optimize or never_applied or not os.path.exists(config.SCORES_PATH)
        if need_opt and os.path.exists(config.DB_PATH):
            try:
                deck_builder.optimize_and_save(page)
            except Exception as e:
                print("Optimisation du deck impossible :", str(e).splitlines()[0])
        else:
            print("Collection inchangée : deck recommandé conservé.")
        ge.load_scores()

        # Étape 2b : construire le deck recommandé dans le Deck Lab s'il n'est pas encore appliqué
        if ours.deck:
            ge.ACTIVE_DECK_NAME = ours.deck
            print(f"Deck imposé : « {ours.deck} » (aucune construction automatique).")
        else:
            try:
                deck_lab.maybe_apply_deck(page, force=ours.build_deck)
                ge.ACTIVE_DECK_NAME = deck_lab.ACTIVE_DECK_NAME
            except KeyboardInterrupt:
                print("\nArrêt demandé pendant la préparation du deck.")
                return
            except Exception as e:
                print("Application du deck impossible :", str(e).splitlines()[0])

        # Étape 3 : parties
        page.goto(config.URL)
        pause(2, 3)
        play_loop(ctx)


if __name__ == "__main__":
    main()