"""
config.py - Configuration constants, selectors, and CLI argument parser.
"""
import argparse
import os
import sys

# Target URL & System Paths
URL = "https://aimasterstcg.com/"
PROFILE_DIR = r"C:\chrome-bot"
CDP = "http://127.0.0.1:9222"

DB_PATH = "cards_db.json"
SCORES_PATH = "card_scores.json"
DECK_FILE = "deck_recommande.txt"
DECK_STATE_PATH = "deck_state.json"
STATS_PATH = "stats.csv"
LOG_PATH = "game_log.txt"

# Default Execution Parameters
MAX_GAMES = 5
STUCK_TIMEOUT = 120
MAX_DEPLOY_PER_TURN = 4
PLAY_PROTOCOLS = False
ATTACK_MARGIN = 0
MARGIN_AUTO = False
MARGIN_RAM_COST = 0.12
BELIEF_SAMPLES = 400
PAUSE_SCALE = 1.0
INTERCEPT_CHUMP_HP = 2
USE_RESERVE = False
KEEP_SUPPORT = 1
GUARD_HOME_HP = 0
DEFENSE_WEIGHT = 0.0

# Deck Optimizer Settings
ITERS = 400
SIMS = 600
PAUSE_ON_NEW_DECK = True
BUILD_DECK = True
SHOW_TURNS = False

# Game Mechanics Constants
SAFE_PROTOCOLS = {"040", "116", "098", "118"}
PROTOCOL_RANGES = [(40, 59), (96, 125)]
MAX_COPIES = 3
SECONDARY_MAX = 10
DECK_SIZE = 30
SIM_TURNS = 7
LEADER_BONUS_TURNS = (3, 5, 7)

# Playwright UI Selectors
SEL_MY_LEADER = 'button.cb-portrait[data-seat="0"][data-fighter="leader"]'
SEL_OPP_LEADER = 'button.cb-portrait[data-seat="1"][data-fighter="leader"]'
SEL_ATTACH = 'button[title="Give this fighter +1 Power for your turn"]'
SEL_HAND = 'button.ah-card[data-hand-owner="self"][data-playable="true"]'
SEL_COMMIT = "button.ah-commit"
SEL_MY_ATTACKERS = ('button[data-seat="0"][aria-label^="Attack with"], '
                    'button.cb-piece[data-seat="0"].cb-pickable')
SEL_TARGETS = 'button[aria-label^="Target"]'

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def parse_args():
    """Parse and apply command-line arguments to global parameters."""
    global MAX_GAMES, PAUSE_ON_NEW_DECK, BUILD_DECK, USE_RESERVE
    global GUARD_HOME_HP, ATTACK_MARGIN, MARGIN_AUTO, PAUSE_SCALE, DEFENSE_WEIGHT

    ap = argparse.ArgumentParser(description="AI Masters TCG Bot Suite")
    ap.add_argument("--games", type=int, default=MAX_GAMES, help="Number of training games")
    ap.add_argument("--no-update", action="store_true", help="Skip card database update")
    ap.add_argument("--force-update", action="store_true", help="Re-open all cards in collection")
    ap.add_argument("--force-optimize", action="store_true", help="Force deck re-optimization")
    ap.add_argument("--no-pause", action="store_true", help="Do not wait for manual deck build input")
    ap.add_argument("--no-build", action="store_true", help="Disable automatic deck building in Deck Lab")
    ap.add_argument("--build-deck", action="store_true", help="Force rebuild of recommended deck in Deck Lab")
    ap.add_argument("--reserve", action="store_true", help="Reserve RAM and Support Protocol for defense")
    ap.add_argument("--no-reserve", action="store_true", help="Disable defense RAM reservation")
    ap.add_argument("--guard-home", type=int, default=GUARD_HOME_HP, help="HP threshold under which Guards do not attack")
    ap.add_argument("--margin", default="0", help="Power margin above opponent Leader defense ('0' or 'auto')")
    ap.add_argument("--defense-weight", type=float, default=DEFENSE_WEIGHT, help="Weight of blocked attack in deck optimization")
    ap.add_argument("--deck", default=None, help="Force specific Arena deck name without building")
    ap.add_argument("--speed", type=float, default=PAUSE_SCALE, help="Pause scale multiplier (e.g. 0.6)")
    ap.add_argument("--report", action="store_true", help="Print statistics summary from stats.csv and exit")

    args = ap.parse_args()

    GUARD_HOME_HP = args.guard_home
    if str(args.margin).lower() == "auto":
        MARGIN_AUTO = True
    else:
        try:
            ATTACK_MARGIN = int(args.margin)
        except ValueError:
            sys.exit("Error: --margin requires 'auto' or an integer value.")
        MARGIN_AUTO = False

    PAUSE_SCALE = max(0.2, args.speed)
    USE_RESERVE = bool(args.reserve) and not args.no_reserve
    DEFENSE_WEIGHT = args.defense_weight
    MAX_GAMES = args.games

    if args.no_pause:
        PAUSE_ON_NEW_DECK = False
    if args.no_build:
        BUILD_DECK = False

    return args