"""
Value scanner: NFL, NBA, college basketball, NHL and college football game lines (moneyline, spread, total) at every book and exchange
you can bet, priced against the sharp market. No model -- the fair line is the market itself.

    python3 scan/scan.py watch [--minutes 330]   loop: free exchange checks every ~3 min, paid confirm pulls on triggers
    python3 scan/scan.py pull [sport]            one paid pull now (all sports if none given)
    python3 scan/scan.py grade                   finals (ESPN, free) -> win/loss, profit, closing-line value

How it decides:
  * Fair line = weighted consensus of every book, each devigged at its own number (Pinnacle 3x, exchanges 2x, others
    1x), leaving out the book being checked. A play must beat that consensus AND Pinnacle alone by the bar (3%; 4% for
    Kalshi / Polymarket and anything more than 6 hours before start -- both closed badly in the NHL price-play test).
  * Free every cycle: Novig, Kalshi and Polymarket prices vs the last fair line. Paid pulls (Odds API, ~3 credits for a
    whole sport: 10 named books x 3 markets) only when something says the fair line may be stale:
      - an exchange price clears the bar against a fair line older than 10 minutes (confirm before alerting)
      - exchanges moved 2.5+ points on a game since the last pull (news; slow books may lag)
      - a logged play starts within 20 minutes (closing line for grading)
      - nothing pulled for 3 hours and games start within 12 hours (baseline)
  * Hard credit cap per month (SCAN_CREDIT_CAP, default 3000) and an even daily share of what's left.

Discord: each game/market/side is posted once, with cents (exchanges) or implied % (books) next to the American odds.
A better number later edits that same message ("Better number at ...", showing the first price) -- no repost.

Storage: Firestore scan_state/* and scan_plays/*. This repo is public: no odds are written to it and the log prints
counts only (prices go to Firestore and Discord). SCAN_DRY=1 keeps state in local files and prints alerts instead.
"""
import argparse, json, math, os, queue, re, threading, time
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
N = NormalDist()
DRY = os.environ.get("SCAN_DRY", "").lower() in ("1", "true", "yes")
POST = os.environ.get("SCAN_POST", "1").lower() in ("1", "true", "yes")
PROJECT = "stock-model-42fb2"
ODDS = "https://api.the-odds-api.com/v4/sports"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
NOVIG = "https://api.novig.com/v3/public"
GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "value-scan", "Accept": "application/json"}

SPORTS = {
    "nfl":   dict(odds="americanfootball_nfl", novig="NFL", poly=["nfl"], label="NFL",
                  kalshi=dict(h2h="KXNFLGAME", spread="KXNFLSPREAD", total="KXNFLTOTAL"),
                  sd=dict(spread=13.5, total=13.5), max_move=0.0),
    "nba":   dict(odds="basketball_nba", novig="NBA", poly=["nba"], label="NBA",
                  kalshi=dict(h2h="KXNBAGAME", spread="KXNBASPREAD", total="KXNBATOTAL"),
                  sd=dict(spread=12.0, total=17.0), max_move=2.5),
    "ncaab": dict(odds="basketball_ncaab", novig="NCAAB", poly=["cbb", "ncaab"], label="CBB",
                  kalshi=dict(h2h="KXNCAAMBGAME", spread="KXNCAAMBSPREAD", total="KXNCAAMBTOTAL"),
                  sd=dict(spread=11.0, total=16.0), max_move=2.5),
    "ncaaf": dict(odds="americanfootball_ncaaf", novig="NCAAF", poly=["cfb"], label="CFB",
                  kalshi=dict(h2h="KXNCAAFGAME", spread="KXNCAAFSPREAD", total="KXNCAAFTOTAL"),
                  sd=dict(spread=15.0, total=15.0), max_move=0.0),   # key numbers: exact lines only, like the NFL
    # soccer: 3-way moneyline (home / away / draw, 90 min + stoppage) and half-goal totals. alerts=False: priced
    # and graded but not posted, until its record says it's worth betting.
    "epl":     dict(odds="soccer_epl", novig="EPL", poly=["epl"], label="EPL", three_way=True, espn="soccer/eng.1",
                    kalshi=dict(h2h="KXEPLGAME", total="KXEPLTOTAL"), sd=dict(total=1.7), max_move=0.0, markets="h2h,totals"),
    "ucl":     dict(odds="soccer_uefa_champs_league", novig=None, poly=["ucl"], label="UCL", three_way=True,
                    espn="soccer/uefa.champions", kalshi=dict(h2h="KXUCLGAME", total="KXUCLTOTAL"), sd=dict(total=1.7),
                    max_move=0.0, markets="h2h,totals"),
    "laliga":  dict(odds="soccer_spain_la_liga", novig=None, poly=["lal"], label="La Liga", three_way=True, alerts=False,
                    espn="soccer/esp.1", kalshi=dict(h2h="KXLALIGAGAME", total="KXLALIGATOTAL"), sd=dict(total=1.7),
                    max_move=0.0, markets="h2h,totals"),
    "seriea":  dict(odds="soccer_italy_serie_a", novig=None, poly=["sea"], label="Serie A", three_way=True, alerts=False,
                    espn="soccer/ita.1", kalshi=dict(h2h="KXSERIEAGAME", total="KXSERIEATOTAL"), sd=dict(total=1.7),
                    max_move=0.0, markets="h2h,totals"),
    "bundes":  dict(odds="soccer_germany_bundesliga", novig=None, poly=["bun"], label="Bundesliga", three_way=True,
                    alerts=False, espn="soccer/ger.1", kalshi=dict(h2h="KXBUNDESLIGAGAME", total="KXBUNDESLIGATOTAL"),
                    sd=dict(total=1.7), max_move=0.0, markets="h2h,totals"),
    "ligue1":  dict(odds="soccer_france_ligue_one", novig=None, poly=["fl1"], label="Ligue 1", three_way=True, alerts=False,
                    espn="soccer/fra.1", kalshi=dict(h2h="KXLIGUE1GAME", total="KXLIGUE1TOTAL"), sd=dict(total=1.7),
                    max_move=0.0, markets="h2h,totals"),
    "mls":     dict(odds="soccer_usa_mls", novig="MLS", poly=["mls"], label="MLS", three_way=True,
                    espn="soccer/usa.1", kalshi=dict(h2h="KXMLSGAME", total="KXMLSTOTAL"), sd=dict(total=1.7),
                    max_move=0.0, markets="h2h,totals"),
    # tennis: the Odds API lists each tournament separately ('tennis_atp_paris_masters'); every active one is pulled
    "atp":     dict(odds=None, odds_prefix="tennis_atp", novig="ATP", poly=["atp"], label="ATP",
                    kalshi=dict(h2h="KXATPMATCH"), sd={}, max_move=0.0, markets="h2h"),
    "wta":     dict(odds=None, odds_prefix="tennis_wta", novig="WTA", poly=["wta"], label="WTA",
                    kalshi=dict(h2h="KXWTAMATCH"), sd={}, max_move=0.0, markets="h2h"),
    "nhl":   dict(odds="icehockey_nhl", novig="NHL", poly=["nhl"], label="NHL",
                  kalshi=dict(h2h="KXNHLGAME", spread="KXNHLSPREAD", total="KXNHLTOTAL"),
                  sd=dict(spread=2.3, total=2.3), max_move=0.0, markets="h2h,totals"),
}
SHARP = "pinnacle"
# 10 named books = 1 region of credits per market (fewer books cost the same). Two jobs:
#   BET_BOOKS -- where the owner bets (the only books alerts can name)
#   the rest  -- pricing only (Pinnacle): sharpens the consensus fair line but is never alerted
# No DraftKings (unfunded), Fanatics or Bovada.
# Kalshi and Polymarket come from their free feeds every minute, so they don't take paid slots.
BOOKS = ["pinnacle", "novig", "prophetx", "fanduel", "betrivers", "ballybet", "betmgm", "betonlineag", "williamhill_us",
         "espnbet"]
BET_BOOKS = {"novig", "prophetx", "kalshi", "polymarket", "fanduel", "betrivers", "ballybet", "betmgm", "betonlineag",
             "williamhill_us", "espnbet"}
WEIGHTS = {"pinnacle": 3.0, "novig": 2.0, "prophetx": 2.0, "kalshi": 2.0, "polymarket": 2.0}
# pregame taker fees, checked on real slips (see nhl-model model_config.json exchange_fees)
FEES = {"kalshi": ("parabolic", 0.07), "polymarket": ("parabolic", 0.07), "prophetx": ("winnings", 0.02)}
FEES_BY_SPORT = {("nhl", "prophetx"): ("parabolic", 0.035)}
BAR = 0.03                 # EV after fees vs consensus and vs Pinnacle
BAR_STRICT = 0.04          # Kalshi / Polymarket, and plays found more than EARLY_H before start
STRICT_BOOKS = {"kalshi", "polymarket"}
EARLY_H = 6
LONGSHOT_P = 0.20          # sides the market gives under 20% also need the stricter bar
TEST_SPORTS = {"nfl", "ncaaf", "nhl", "nba"}   # sharp markets: 1%-to-bar plays are logged + graded (not posted) as a test
TEST_MIN = 0.01
OVERCAP_MAX = 0.30         # 12-30% "edges" (above MAX_EV) are logged + graded, not posted, to test the cap
POST_WITHIN_H = float(os.environ.get("SCAN_POST_WITHIN_H", "12"))   # plays further out are logged + graded, not posted
MAX_EV = 0.12              # above this it's a stale quote or a mismatched game, not value
MIN_BOOKS = 3              # books left in the consensus after removing the one being checked
MIN_SIZE_USD = 25          # exchange liquidity at the quoted price
REPOST_CENTS = 10          # alert the same play again only if the price improved this much
BANKROLL = float(os.environ.get("SCAN_BANKROLL", "2000"))
EXPOSURE_CAP = float(os.environ.get("SCAN_EXPOSURE", "0"))   # 0 = off. If set (e.g. 0.5): over that share pending, only 5%+ EV posts
EXPOSURE_OVERRIDE_EV = 0.05
KELLY = 1 / 8
MAX_STAKE = 0.02


def bet_size(usd):
    """1/8 Kelly in dollars, to the dollar ($1 minimum) -- no other rounding or limits."""
    return max(1, round(usd))
HOURS_AHEAD = 24
CREDIT_CAP = int(os.environ.get("SCAN_CREDIT_CAP", "8000"))
CYCLE_S = 60               # moneylines every minute (free) ...
FULL_EVERY = 3             # ... spreads and totals every 3rd cycle
STALE_FAIR_MIN = 10        # an exchange play against a fair line older than this is confirmed by a pull first
MOVE_TRIGGER = 0.025
BASE_GAP_MIN = 180
MIN_PULL_GAP_MIN = 15
CLOSE_WINDOW_MIN = 20
START_BUFFER_MIN = 3       # no plays this close to (or after) the start: prices turn live and the fair line is pregame


def log(*a):
    print(datetime.now(ET).strftime("%H:%M:%S"), *a, flush=True)


# ---------------- prices ----------------
def prob(odds):
    odds = float(odds)
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def american(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def fee_for(sport, book):
    return FEES_BY_SPORT.get((sport, book)) or FEES.get(book)


def payout(odds, book, sport):
    """Profit per 1 risked, after the exchange fee."""
    odds = float(odds)
    pay = odds / 100 if odds > 0 else 100 / -odds
    kind, rate = fee_for(sport, book) or (None, 0)
    if kind == "parabolic":
        p = 1 / (1 + pay)
        return 1 / (p + rate * p * (1 - p)) - 1
    return pay * (1 - rate) if kind else pay


def mover(sport, kind, line, q):
    """A book's devigged chance q that side A wins at `line` -> fn(new_line). spread: side A = home, line = home
    handicap; total: side A = over."""
    if kind == "h2h":
        return lambda new: q
    z = N.inv_cdf(min(max(q, 1e-4), 1 - 1e-4))
    sd = SPORTS[sport]["sd"][kind]
    if kind == "spread":
        mu = sd * z - line
        return lambda new: N.cdf((mu + new) / sd)
    mu = line + sd * z
    return lambda new: 1 - N.cdf((new - mu) / sd)


def sides_of(row):
    return ("a", "b", "d") if "d" in row else ("a", "b")


def quotes(event, three_way=False):
    """{kind: [{book, line, a, b[, d]}]}, a = home / over, b = away / under, d = draw (soccer moneyline)."""
    home, away = event["home_team"], event["away_team"]
    out = {"h2h": [], "spread": [], "total": []}
    for bm in event.get("bookmakers", []):
        for mk in bm.get("markets", []):
            o = {x["name"]: x for x in mk.get("outcomes", [])}
            if mk["key"] == "h2h" and three_way and home in o and away in o and "Draw" in o and len(o) == 3:
                out["h2h"].append(dict(book=bm["key"], line=None, a=o[home]["price"], b=o[away]["price"], d=o["Draw"]["price"]))
            elif mk["key"] == "h2h" and not three_way and home in o and away in o and len(o) == 2:
                out["h2h"].append(dict(book=bm["key"], line=None, a=o[home]["price"], b=o[away]["price"]))
            elif mk["key"] == "spreads" and home in o and away in o and o[home].get("point") == -o[away].get("point", 1e9):
                out["spread"].append(dict(book=bm["key"], line=o[home]["point"], a=o[home]["price"], b=o[away]["price"]))
            elif mk["key"] == "totals" and "Over" in o and "Under" in o and o["Over"].get("point") == o["Under"].get("point"):
                out["total"].append(dict(book=bm["key"], line=o["Over"]["point"], a=o["Over"]["price"], b=o["Under"]["price"]))
    for k, rows in out.items():   # drop dead quotes and lines far from the pack
        rows = [r for r in rows if all(100 <= abs(float(r[s])) <= 1500 for s in sides_of(r))]
        if three_way and k == "total":   # soccer: half-goal lines only (quarter lines half-push)
            rows = [r for r in rows if r["line"] is not None and (r["line"] * 2) % 2 == 1]
        if k != "h2h" and rows:
            mid = sorted(r["line"] for r in rows)[len(rows) // 2]
            rows = [r for r in rows if abs(r["line"] - mid) <= 3]
        out[k] = rows
    return out


def devig(ia, ib):
    """Power method: fair p = implied ** k with k chosen so the two sides sum to 1. Same as the proportional method on
    even lines, but on lopsided ones it puts the book's margin on the longshot, where books actually load it
    (proportional devig makes big underdogs look more likely than they are)."""
    if ia + ib <= 1:
        return ia / (ia + ib)
    lo, hi = 1.0, 3.0
    for _ in range(50):
        k = (lo + hi) / 2
        if ia ** k + ib ** k > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    return ia ** k / (ia ** k + ib ** k)


def devig_n(imps):
    """Power-method devig for any number of outcomes (soccer: home / away / draw)."""
    if sum(imps) <= 1:
        return [x / sum(imps) for x in imps]
    lo, hi = 1.0, 3.0
    for _ in range(50):
        k = (lo + hi) / 2
        if sum(x ** k for x in imps) > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    t = sum(x ** k for x in imps)
    return [x ** k / t for x in imps]


def fair_views(sport, kind, rows):
    """Compact per-book views saved with each game: [book, weight, line, devigged P(side A)] -- or, for a 3-way
    moneyline, [book, weight, None, [P(home), P(away), P(draw)]]."""
    out = []
    for r in rows:
        if "d" in r:
            q = [round(x, 5) for x in devig_n([prob(r["a"]), prob(r["b"]), prob(r["d"])])]
        else:
            q = round(devig(prob(r["a"]), prob(r["b"])), 5)
        out.append([r["book"], WEIGHTS.get(r["book"], 1.0), r["line"], q])
    return out


SIDE_IX = {"a": 0, "b": 1, "d": 2}


def fair_side(sport, kind, views, line, side, without=None, only=None):
    """Consensus chance that `side` wins at `line` (2-way or 3-way), or None if too few books."""
    if views and isinstance(views[0][3], list):
        use = [(w, q[SIDE_IX[side]]) for bk, w, l0, q in views if bk != without and (only is None or bk == only)]
        if not use or (only is None and len(use) < MIN_BOOKS):
            return None
        return sum(w * q for w, q in use) / sum(w for w, _ in use)
    if side == "d":
        return None
    pa = fair_at(sport, kind, views, line, without=without, only=only)
    return None if pa is None else (pa if side == "a" else 1 - pa)


def fair_at(sport, kind, views, line, without=None, only=None):
    """Consensus P(side A) at `line` from saved views, or None if too few books."""
    mm = SPORTS[sport]["max_move"]
    best = {}   # each book once: its view at (or nearest to) the line being priced
    for bk, w, l0, q in views:
        if bk == without or (only is not None and bk != only):
            continue
        d = 0.0 if kind == "h2h" else abs(l0 - line)
        if d <= mm + 1e-9 and (bk not in best or d < best[bk][0]):
            best[bk] = (d, w, l0, q)
    use = [(w, mover(sport, kind, l0, q)) for d, w, l0, q in best.values()]
    if not use or (only is None and len(use) < MIN_BOOKS):
        return None
    return sum(w * fn(line) for w, fn in use) / sum(w for w, _ in use)


SPORT_BAR = {"nfl": 0.02, "ncaaf": 0.02, "nhl": 0.02, "ncaab": 0.02, "nba": 0.02}   # sharp markets + CBB (logger: 2% bar, +2% CLV)


def _fair_w(sport, kind, views, line, side, without, weight):
    """Like fair_side, but with a custom weight per (book, base weight) -- for the logged fair-line variants."""
    if not views:
        return None
    if isinstance(views[0][3], list):
        use = [(weight(bk, w), q[SIDE_IX[side]]) for bk, w, l0, q in views if bk != without]
        use = [(w, q) for w, q in use if w > 0]
        if len(use) < MIN_BOOKS:
            return None
        return sum(w * q for w, q in use) / sum(w for w, _ in use)
    if side == "d":
        return None
    mm = SPORTS[sport]["max_move"]
    best = {}
    for bk, w, l0, q in views:
        if bk == without:
            continue
        d = 0.0 if kind == "h2h" else abs(l0 - line)
        if d <= mm + 1e-9 and (bk not in best or d < best[bk][0]):
            best[bk] = (d, weight(bk, w), l0, q)
    use = [(w, mover(sport, kind, l0, q)) for d, w, l0, q in best.values() if w > 0]
    if len(use) < MIN_BOOKS:
        return None
    pa = sum(w * fn(line) for w, fn in use) / sum(w for w, _ in use)
    return pa if side == "a" else 1 - pa


FRESH_HALF_MIN = 30        # 'fresh' variant: a book's weight halves for every 30 minutes since its last update


def fair_variants(sport, g, kind, side, line, book, now):
    """The fair chance for this side under each candidate method, for the side-by-side test (vs Pinnacle's close):
    live (what alerts use) · pin (Pinnacle only) · equal (every book weight 1) · fresh (stale prices discounted)
    · exblend (live averaged with the exchanges' live bid/ask midpoint, when there is one)."""
    views = (g.get("views") or {}).get(kind)
    if not views:
        return {}
    upd = (g.get("upd") or {}).get(kind) or {}

    def age_w(bk, w):
        t = upd.get(bk) or g.get("at")
        try:
            age = (now - datetime.fromisoformat(t.replace("Z", "+00:00"))).total_seconds() / 60
        except (AttributeError, ValueError):
            return w
        return w * 0.5 ** (max(age, 0) / FRESH_HALF_MIN)
    out = dict(live=fair_side(sport, kind, views, line, side, without=book),
               pin=fair_side(sport, kind, views, line, side, only=SHARP),
               equal=_fair_w(sport, kind, views, line, side, book, lambda bk, w: 1.0),
               fresh=_fair_w(sport, kind, views, line, side, book, age_w))
    mid = (g.get("exmid") or {}).get(f"{kind}|{line}")
    if mid and out["live"] is not None and (now - datetime.fromisoformat(mid[1])).total_seconds() < 600 and side in ("a", "b"):
        m = mid[0] if side == "a" else 1 - mid[0]
        out["exblend"] = (out["live"] + m) / 2
    return {k: round(v, 4) for k, v in out.items() if v is not None}


def exchange_mids(games, quotes_, now):
    """Live bid/ask midpoint per game / market / line from the exchanges' two-sided prices (for the exblend variant)."""
    by = {}
    for x in quotes_:
        if x["side"] in ("a", "b"):
            by.setdefault((x["gid"], x["kind"], x["line"], x["book"]), {})[x["side"]] = x["prob"]
    agg = {}
    for (gid, kind, line, bk), v in by.items():
        if "a" in v and "b" in v and 0.98 <= v["a"] + v["b"] <= 1.15:
            agg.setdefault((gid, kind, line), []).append((v["a"] + 1 - v["b"]) / 2)
    for (gid, kind, line), mids in agg.items():
        g = games.get(gid)
        if g is not None:
            g.setdefault("exmid", {})[f"{kind}|{line}"] = (round(sum(mids) / len(mids), 4), now.isoformat())


def bar_for(book, hours_to_start, sport=None):
    if book in STRICT_BOOKS or hours_to_start > EARLY_H:
        return BAR_STRICT
    return SPORT_BAR.get(sport, BAR)


def evaluate(sport, g, kind, side, line, book, odds, now, price_prob=None, allow_test=False):
    """A play dict if (book, odds) on this side clears the bar, else None. g = saved game (with views)."""
    views = (g.get("views") or {}).get(kind)
    if not views:
        return None
    pw = fair_side(sport, kind, views, line, side, without=book)
    ppw = fair_side(sport, kind, views, line, side, only=SHARP)
    if pw is None or ppw is None:
        return None
    pay = payout(odds, book, sport)
    ev, ev_pin = pw * pay - (1 - pw), ppw * pay - (1 - ppw)
    hrs = (datetime.fromisoformat(g["start"]) - now).total_seconds() / 3600
    if hrs * 60 < START_BUFFER_MIN:
        return None
    bar = bar_for(book, hrs, sport)
    if pw < LONGSHOT_P:
        bar = max(bar, BAR_STRICT)
    test = overcap = False
    if not (bar <= ev <= MAX_EV and ev_pin >= bar):
        if allow_test and MAX_EV < ev <= OVERCAP_MAX and ev_pin >= bar:
            test = overcap = True     # above the cap: usually stale or mismatched -- logged to prove it either way
        elif allow_test and sport in TEST_SPORTS and TEST_MIN <= ev < bar and ev_pin >= TEST_MIN:
            test = True               # sharp-market test: 1%+ vs consensus AND Pinnacle, under the bar
        else:
            return None
    pin = [l0 for bk, w, l0, q in views if bk == SHARP and l0 is not None]
    off_main = round(abs(line - pin[0]), 1) if (pin and line is not None) else 0.0
    kelly = ev / pay
    stake = min(kelly * KELLY, MAX_STAKE)
    to_win = bet_size(stake * BANKROLL) * pay
    # worst price that still clears the bar vs the consensus (fees ignored for the display number)
    good = american(pw / (1 + bar))
    return dict(sport=sport, kind=kind, side=side, line=line, book=book, odds=int(odds), label=label(g, kind, side, line),
                p_fair=round(pw, 4), fair_odds=american(pw), ev=round(ev, 4), ev_pin=round(ev_pin, 4),
                stake_pct=round(stake, 4), stake_usd=bet_size(stake * BANKROLL), to_win=round(to_win, 2), good_to=good, bar=bar,
                price_prob=round(price_prob if price_prob is not None else prob(odds), 4), off_main=off_main,
                **({"test": True} if test else {}), **({"overcap": True} if overcap else {}))


def label(g, kind, side, line):
    if kind == "h2h" and side == "d":
        return "Draw (90 min)"
    if kind == "h2h":
        return f"{g['home'] if side == 'a' else g['away']} ML" + (" (90 min)" if g.get("three_way") else "")
    if kind == "spread":
        return f"{g['home']} {line:+g}" if side == "a" else f"{g['away']} {-line:+g}"
    return f"{'Over' if side == 'a' else 'Under'} {line:g}"


# ---------------- Firestore ----------------
def _val(v):
    if v is None: return {"nullValue": None}
    if isinstance(v, bool): return {"booleanValue": v}
    if isinstance(v, int): return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"nullValue": None} if not math.isfinite(v) else {"doubleValue": v}
    if isinstance(v, dict): return {"mapValue": {"fields": {str(k): _val(x) for k, x in v.items()}}}
    if isinstance(v, (list, tuple)): return {"arrayValue": {"values": [_val(x) for x in v]}}
    return {"stringValue": str(v)}


def _unval(v):
    if "mapValue" in v: return {k: _unval(x) for k, x in v["mapValue"].get("fields", {}).items()}
    if "arrayValue" in v: return [_unval(x) for x in v["arrayValue"].get("values", [])]
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue" in v: return v["doubleValue"]
    if "booleanValue" in v: return v["booleanValue"]
    if "nullValue" in v: return None
    return v.get("stringValue", v.get("timestampValue"))


_TOKEN = [None, 0.0]


def token():
    if _TOKEN[0] is None or time.time() - _TOKEN[1] > 45 * 60:   # tokens last an hour; the watch loop runs longer
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        info = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
        creds = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/datastore"])
        creds.refresh(Request())
        _TOKEN[:] = [creds.token, time.time()]
    return _TOKEN[0]


def doc_url(path):
    return f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents/{path}"


DRY_DIR = os.environ.get("SCAN_DRY_DIR", "/tmp/scan_dry")


def _dry_file(path):
    return os.path.join(DRY_DIR, path.replace("/", "__") + ".json")


def fs_get(path):
    if DRY:
        f = _dry_file(path)
        return json.load(open(f)) if os.path.exists(f) else None
    r = requests.get(doc_url(path), headers={"Authorization": f"Bearer {token()}"}, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return {k: _unval(v) for k, v in r.json().get("fields", {}).items()}


def fs_set(path, data):
    if DRY:
        os.makedirs(DRY_DIR, exist_ok=True)
        json.dump(data, open(_dry_file(path), "w"), default=str)
        return
    body = {"fields": {k: _val(v) for k, v in data.items()}}
    for wait in (2, 5, 15, None):
        r = requests.patch(doc_url(path), headers={"Authorization": f"Bearer {token()}"}, json=body, timeout=30)
        if wait is None or (r.status_code != 429 and r.status_code < 500):
            break
        time.sleep(wait)
    r.raise_for_status()


def fs_list(collection):
    if DRY:
        import glob
        pre = collection + "__"
        return [{"_id": os.path.basename(f)[len(pre):-5], **json.load(open(f))}
                for f in glob.glob(os.path.join(DRY_DIR, pre + "*.json"))]
    out, page = [], None
    while True:
        params = {"pageSize": 300, **({"pageToken": page} if page else {})}
        r = requests.get(doc_url(collection), headers={"Authorization": f"Bearer {token()}"}, params=params, timeout=30)
        r.raise_for_status()
        d = r.json()
        out += [{"_id": x["name"].rsplit("/", 1)[-1], **{k: _unval(v) for k, v in x.get("fields", {}).items()}}
                for x in d.get("documents", [])]
        page = d.get("nextPageToken")
        if not page:
            return out


def fs_recent(collection, field, since_iso):
    """Documents whose `field` (an ISO string) is >= since_iso -- keeps reads flat as the play log grows."""
    if DRY:
        return [d for d in fs_list(collection) if (d.get(field) or "") >= since_iso]
    q = {"structuredQuery": {"from": [{"collectionId": collection}],
                             "where": {"fieldFilter": {"field": {"fieldPath": field}, "op": "GREATER_THAN_OR_EQUAL",
                                                       "value": {"stringValue": since_iso}}}}}
    url = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents:runQuery"
    r = requests.post(url, headers={"Authorization": f"Bearer {token()}"}, json=q, timeout=60)
    r.raise_for_status()
    return [{"_id": x["document"]["name"].rsplit("/", 1)[-1],
             **{k: _unval(v) for k, v in x["document"].get("fields", {}).items()}}
            for x in r.json() if "document" in x]


def play_id(p, event_id):
    line = "ml" if p["line"] is None else f"{p['line']:g}"
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", f"{p['sport']}_{event_id}_{p['kind']}_{p['side']}_{line}_{p['book']}")


# ---------------- credits ----------------
def credits_state(now):
    st = fs_get("scan_state/credits") or {}
    month = now.strftime("%Y-%m")
    if st.get("month") != month:
        st = dict(month=month, used=0, days={})
    return st


def can_spend(st, now, cost=3):
    """Monthly hard cap, and today's share of what's left (so one busy day can't eat the month)."""
    if st["used"] + cost > CREDIT_CAP:
        return False
    nxt = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
    days_left = max((nxt - now).days, 1)
    today = now.astimezone(ET).strftime("%Y-%m-%d")
    spent_today = st["days"].get(today, 0)
    allowance = (CREDIT_CAP - st["used"] + spent_today) / days_left
    return spent_today + cost <= max(allowance, 12)


def charge(st, now, cost):
    st["used"] += cost
    today = now.astimezone(ET).strftime("%Y-%m-%d")
    st["days"] = {k: v for k, v in st["days"].items() if k >= (now - timedelta(days=10)).strftime("%Y-%m-%d")}
    st["days"][today] = st["days"].get(today, 0) + cost
    fs_set("scan_state/credits", st)


# ---------------- book scorecard ----------------
def book_tally(sport, events_q, now):
    """For every priced side in a pull: which book had the best price and how far each book was from fair (after
    fees). Accumulated per month / sport / book in Firestore -- answers 'which books are worth funding' for every
    sport, from data the pull already paid for."""
    tally = {}
    for g, q in events_q:
        for kind, rows in q.items():
            if not rows or not (g.get("views") or {}).get(kind):
                continue
            for line in {r["line"] for r in rows}:
                at = [r for r in rows if r["line"] == line and r["book"] != SHARP]
                for side in (("a", "b", "d") if at and "d" in at[0] else ("a", "b")):
                    pays = {r["book"]: payout(r[side], r["book"], sport) for r in at if side in r}
                    if not pays:
                        continue
                    best = max(pays.values())
                    for r in at:
                        if r["book"] not in pays:
                            continue
                        pw = fair_side(sport, kind, g["views"][kind], line, side, without=r["book"])
                        if pw is None:
                            continue
                        ev = pw * (1 + pays[r["book"]]) - 1
                        t = tally.setdefault(r["book"], [0, 0, 0, 0, 0.0])
                        t[0] += 1
                        t[1] += pays[r["book"]] >= best - 1e-9
                        t[2] += ev >= 0.02
                        t[3] += ev >= 0.03
                        t[4] += ev
    if not tally:
        return
    path = f"scan_state/books_{now.strftime('%Y-%m')}"
    doc = fs_get(path) or {}
    sp = doc.get(sport) or {}
    for bk, t in tally.items():
        old = sp.get(bk) or [0, 0, 0, 0, 0.0]
        sp[bk] = [old[i] + t[i] for i in range(5)]
    doc[sport] = sp
    fs_set(path, doc)


def cbb_book_report():
    """The CBB logger's graded plays (cbb_plays) by book: count, record, units, closing-line value. Totals only."""
    ps = [p for p in fs_list("cbb_plays") if p.get("result") in ("win", "loss", "push")]
    if not ps:
        return "no graded CBB logger plays"
    out = [f"CBB logger, {len(ps)} graded plays (every book listed, so one game/side can appear at several books)",
           "  book         plays    W-L      units   ROI    avg CLV  beat close"]
    by = {}
    for p in ps:
        by.setdefault(p["book"], []).append(p)
    for bk, xs in sorted(by.items(), key=lambda x: -len(x[1])):
        w = sum(p["result"] == "win" for p in xs); l = sum(p["result"] == "loss" for p in xs)
        u = sum(p.get("profit") or 0 for p in xs)
        c = [p["ev_close"] for p in xs if p.get("ev_close") is not None]
        clv = f"{sum(c) / len(c):+6.2%} {sum(x > 0 for x in c) / len(c):7.0%}" if c else "     -        -"
        out.append(f"  {BOOK_NAMES.get(bk, bk):11s} {len(xs):5d} {w:4d}-{l:<4d} {u:+7.1f} {u / len(xs):+6.1%}  {clv}")
    for kind in ("h2h", "spread", "total"):
        xs = [p for p in ps if p.get("kind") == kind]
        if xs:
            c = [p["ev_close"] for p in xs if p.get("ev_close") is not None]
            out.append(f"  market {kind:7s} {len(xs):4d} plays, {sum(p.get('profit') or 0 for p in xs):+.1f}u"
                       + (f", avg CLV {sum(c) / len(c):+.2%}" if c else ""))
    return "\n".join(out)


def book_report(now):
    """Plain-text scorecard (aggregates only -- safe for the public log)."""
    doc = fs_get(f"scan_state/books_{now.strftime('%Y-%m')}") or {}
    out = []
    for sport in SPORTS:
        sp = doc.get(sport)
        if not sp:
            continue
        out.append(f"{SPORTS[sport]['label']}: book / sides priced / best price / 2%+ value / 3%+ value / avg vs fair")
        for bk, t in sorted(sp.items(), key=lambda x: -x[1][1] / max(x[1][0], 1)):
            if t[0]:
                out.append(f"  {BOOK_NAMES.get(bk, bk):11s} {t[0]:6d} {t[1] / t[0]:6.0%} {t[2] / t[0]:6.1%} {t[3] / t[0]:6.1%} {t[4] / t[0]:+6.2%}")
    return "\n".join(out)


BOOK_Q, BOOK_Q_AT = {}, {}   # sport -> {(gid, kind, side, line, book): odds} from the last pull, and its time
_ACTIVE = [None, 0.0]        # Odds API sports list (free to read), refreshed every 6 hours


def odds_keys(sport, key):
    """The Odds API sport keys to pull. Tennis is listed per tournament ('tennis_atp_paris'): every active one."""
    pre = SPORTS[sport].get("odds_prefix")
    if not pre:
        return [SPORTS[sport]["odds"]]
    if _ACTIVE[0] is None or time.time() - _ACTIVE[1] > 6 * 3600:
        try:
            _ACTIVE[0] = [x["key"] for x in requests.get(f"{ODDS}", params=dict(apiKey=key), timeout=30).json() if x.get("active")]
            _ACTIVE[1] = time.time()
        except Exception as e:  # noqa: BLE001
            log(f"sports list: {type(e).__name__}")
            return []
    return [k for k in _ACTIVE[0] if k.startswith(pre)]


# ---------------- alternate lines (per game, Pinnacle's own alternates as the fair price) ----------------
ALT_SPORTS = {"nfl", "ncaaf", "nhl", "nba"}
ALT_WINDOW_H = 6           # only games starting within this many hours (when alerts post)
ALT_REFRESH_MIN = 120
ALT_Q, ALT_Q_AT = {}, {}   # sport -> {(gid, kind, side, line, book): odds} from alternate pulls (kept apart from BOOK_Q)


def alt_rows(event, three_way=False):
    """{'spread': [...], 'total': [...]} rows from alternate_spreads / alternate_totals (each line a row)."""
    home, away = event["home_team"], event["away_team"]
    out = {"spread": [], "total": []}
    for bm in event.get("bookmakers", []):
        for mk in bm.get("markets", []):
            if mk["key"] == "alternate_spreads":
                hb = {o["point"]: o["price"] for o in mk["outcomes"] if o.get("name") == home and o.get("point") is not None}
                ab = {o["point"]: o["price"] for o in mk["outcomes"] if o.get("name") == away and o.get("point") is not None}
                for h, pr in hb.items():
                    if -h in ab:
                        out["spread"].append(dict(book=bm["key"], line=h, a=pr, b=ab[-h]))
            elif mk["key"] == "alternate_totals":
                ov = {o["point"]: o["price"] for o in mk["outcomes"] if o.get("name") == "Over" and o.get("point") is not None}
                un = {o["point"]: o["price"] for o in mk["outcomes"] if o.get("name") == "Under" and o.get("point") is not None}
                for L, pr in ov.items():
                    if L in un:
                        out["total"].append(dict(book=bm["key"], line=L, a=pr, b=un[L]))
    for k in out:
        out[k] = [r for r in out[k] if all(100 <= abs(float(r[x])) <= 1500 for x in ("a", "b"))]
    return out


def merge_alt_views(S):
    """Put saved alternate-line views back into the games (a main pull replaces the game list)."""
    for gid, kinds in S.get("alt_views", {}).items():
        g = S["games"].get(gid)
        if not g:
            continue
        for kind, vs in kinds.items():
            have = {(v[0], v[2]) for v in g.setdefault("views", {}).setdefault(kind, [])}
            g["views"][kind] += [v for v in vs if (v[0], v[2]) not in have]


def pull_alts(sport, S, now, st):
    """Alternate spreads / totals for games starting soon: their views join the fair line (Pinnacle prices each of
    its alternates itself -- no half-point estimate), and every book's alternate at those numbers is checked."""
    key = os.environ.get("ODDS_API_KEY")
    okey = SPORTS[sport].get("odds")
    if not key or not okey or sport not in ALT_SPORTS:
        return []
    S.setdefault("alt_at", {})
    S.setdefault("alt_views", {})
    plays, cost, n = [], 0, 0
    for gid, g in list(S["games"].items()):
        st_ = datetime.fromisoformat(g["start"])
        if not (now < st_ < now + timedelta(hours=ALT_WINDOW_H)):
            continue
        last = S["alt_at"].get(gid)
        if last and (now - datetime.fromisoformat(last)).total_seconds() < ALT_REFRESH_MIN * 60:
            continue
        if not can_spend(st, now, 2):
            break
        r = requests.get(f"{ODDS}/{okey}/events/{gid}/odds", timeout=30, params=dict(
            apiKey=key, bookmakers=",".join(BOOKS), oddsFormat="american", markets="alternate_spreads,alternate_totals"))
        S["alt_at"][gid] = now.isoformat()
        if r.status_code != 200:
            continue
        c = int(r.headers.get("x-requests-last") or 2)
        cost += c
        charge(st, now, c)
        rows = alt_rows(r.json())
        if not any(x["book"] == SHARP for v in rows.values() for x in v):
            continue          # no Pinnacle alternates: nothing trustworthy to price against
        n += 1
        S["alt_views"][gid] = {k: fair_views(sport, k, v) for k, v in rows.items() if v}
        merge_alt_views({"games": {gid: g}, "alt_views": {gid: S["alt_views"][gid]}})
        aq = ALT_Q.setdefault(sport, {})
        for k_ in [k_ for k_ in aq if k_[0] == gid]:
            del aq[k_]
        ALT_Q_AT[(sport, gid)] = now
        for kind, rs in rows.items():
            for row in rs:
                if row["book"] not in BET_BOOKS:
                    continue
                for side in ("a", "b"):
                    aq[(gid, kind, side, row["line"], row["book"])] = int(row[side])
                    p = evaluate(sport, g, kind, side, row["line"], row["book"], row[side], now, allow_test=True)
                    if p:
                        plays.append((gid, g, p, None))
    if n:
        log(f"[{sport}] alternate lines: {n} game(s), {cost} credits, {len(plays)} play(s)")
    return plays


# ---------------- paid pull ----------------
def pull(sport, now, st, reason):
    """One Odds API pull for a sport: saves fair views for every game in the next HOURS_AHEAD, returns
    (games, plays from the books in the pull)."""
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        log(f"[{sport}] no ODDS_API_KEY -- skipping pull")
        return None, []
    if not can_spend(st, now):
        log(f"[{sport}] credit budget reached (month {st['used']}/{CREDIT_CAP}) -- no pull ({reason})")
        return None, []
    params = dict(apiKey=key, bookmakers=",".join(BOOKS), markets=SPORTS[sport].get("markets", "h2h,spreads,totals"),
                  oddsFormat="american",
                  commenceTimeFrom=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  commenceTimeTo=(now + timedelta(hours=HOURS_AHEAD)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    okeys = odds_keys(sport, key)
    if not okeys:
        return {}, []            # e.g. no tennis tournament on right now
    events, cost = [], 0
    for ok in okeys:
        r = requests.get(f"{ODDS}/{ok}/odds", params=params, timeout=60)
        if r.status_code in (404, 422):
            continue
        r.raise_for_status()
        c = int(r.headers.get("x-requests-last") or 0)
        cost += c
        evs = r.json()
        for e in evs:
            e["_okey"] = ok
        events += evs
    charge(st, now, cost)
    games, plays, for_tally = {}, [], []
    bq = BOOK_Q.setdefault(sport, {})
    bq.clear()
    BOOK_Q_AT[sport] = now
    for ev in events:
        three = bool(SPORTS[sport].get("three_way"))
        q = quotes(ev, three)
        if not any(r_["book"] == SHARP for rows in q.values() for r_ in rows):
            continue
        upd = {}
        for bm in ev.get("bookmakers", []):
            for mk in bm.get("markets", []):
                k_ = {"h2h": "h2h", "spreads": "spread", "totals": "total"}.get(mk["key"])
                if k_ and (mk.get("last_update") or bm.get("last_update")):
                    upd.setdefault(k_, {})[bm["key"]] = mk.get("last_update") or bm.get("last_update")
        g = dict(home=ev["home_team"], away=ev["away_team"], start=ev["commence_time"].replace("Z", "+00:00"),
                 at=now.isoformat(), views={k: fair_views(sport, k, rows) for k, rows in q.items() if rows},
                 okey=ev.get("_okey"), upd=upd, **({"three_way": True} if three else {}))
        games[ev["id"]] = g
        for_tally.append((g, q))
        for kind, rows in q.items():
            for row in rows:
                if row["book"] in BET_BOOKS:
                    for side in sides_of(row):
                        bq[(ev["id"], kind, side, row["line"], row["book"])] = int(row[side])
        for kind, rows in q.items():
            for row in rows:
                if row["book"] not in BET_BOOKS:
                    continue
                for side in sides_of(row):
                    p = evaluate(sport, g, kind, side, row["line"], row["book"], row[side], now, allow_test=True)
                    if p:
                        plays.append((ev["id"], g, p, None))
    try:
        book_tally(sport, for_tally, now)
    except Exception as e:  # noqa: BLE001 -- the scorecard never blocks alerts
        log(f"[{sport}] book tally: {type(e).__name__}")
    log(f"[{sport}] pull ({reason}): {len(games)} games, {cost} credits (month {st['used']}/{CREDIT_CAP})")
    return games, plays


# ---------------- free exchange feeds ----------------
def _get(url, params=None):
    for i in range(3):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=25)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 * (i + 1)); continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == 2:
                raise
            time.sleep(2 * (i + 1))
    return None


def norm(name):
    n = (name or "").lower().replace("&", "and").replace("'", "").replace("é", "e")
    n = re.sub(r"\bstate\b", "st", n)
    n = re.sub(r"\bsaint\b", "st", n)
    return re.sub(r"[^a-z0-9]", "", n)


def name_hits(name, full):
    n, f = norm(name), norm(full)
    return bool(n) and len(n) >= 3 and (f.startswith(n) or f.endswith(n) or (len(f) >= 5 and n.startswith(f)))


_MATCH = {}


def find_game(games, t1, t2, day=None, start=None):
    """(gid, game, t1_is_home) for two exchange team names, if exactly one saved game has both teams.
    Remembered per game list, so each venue's games are fuzzy-matched once, not every cycle."""
    key = (frozenset(games), norm(t1), norm(t2), day, start.strftime("%Y%m%d%H") if start else None)
    if key not in _MATCH:
        if len(_MATCH) > 50000:
            _MATCH.clear()
        hit = _find_game(games, t1, t2, day, start)
        _MATCH[key] = (hit[0], hit[2]) if hit else None
    m = _MATCH[key]
    # only the game id and side are remembered: the game itself (fair line) always comes from the current list
    return (m[0], games[m[0]], m[1]) if m and m[0] in games else None


def _find_game(games, t1, t2, day=None, start=None):
    hits = []
    for gid, g in games.items():
        if day and datetime.fromisoformat(g["start"]).astimezone(ET).strftime("%Y-%m-%d") != day:
            continue
        if start and abs((datetime.fromisoformat(g["start"]) - start).total_seconds()) > 6 * 3600:
            continue
        h1, a1 = name_hits(t1, g["home"]), name_hits(t1, g["away"])
        h2, a2 = name_hits(t2, g["home"]), name_hits(t2, g["away"])
        if h1 and not a1 and a2 and not h2:
            hits.append((gid, g, True))
        elif a1 and not h1 and h2 and not a2:
            hits.append((gid, g, False))
    return hits[0] if len(hits) == 1 else None


def ex_quote(gid, g, kind, side, line, book, prob_, size_usd, start=None):
    """start = the venue's own start time, when it lists one (a game moved earlier still counts as started)."""
    return dict(gid=gid, g=g, kind=kind, side=side, line=line, book=book, odds=american(prob_), prob=prob_, size=size_usd,
                start=start)


def novig_quotes(sport, games, ml_only=False):
    lg = SPORTS[sport]["novig"]
    ev_names = {}
    for e in (_get(f"{NOVIG}/catalog/events", dict(league=lg)) or {}).get("items", []):
        mt = re.match(r"(.+?)\s+@\s+(.+?)(?:\s+(?:Qualification|Round of \d+|Round \d+|Quarterfinals?|Semifinals?|"
                      r"Finals?|R\d+))?$", e.get("description") or "")
        if mt:
            ev_names[e["eventId"]] = (mt.group(1), mt.group(2))   # (away, home)
    markets = []
    for mtype in (("MONEY", "TOTAL") if ml_only else ("MONEY", "SPREAD", "TOTAL")):   # TOTAL names home/away
        after = None
        for _ in range(10):
            d = _get(f"{NOVIG}/catalog/markets", dict(league=lg, marketType=mtype, **({"after": after} if after else {})))
            markets += [m for m in d.get("items", []) if m.get("status") == "OPEN" and len(m.get("outcomes", [])) == 2]
            after = d.get("next")
            if not after:
                break
    # abbreviation -> side, from TOTAL descriptions 'DAL @ UTA t219.5'
    abbr = {}
    for m in markets:
        mt = re.match(r"(\S+)\s+@\s+(\S+)\s+t", m.get("description") or "")
        if m["marketType"] == "TOTAL" and mt:
            abbr[m["eventId"]] = {mt.group(1): "away", mt.group(2): "home"}
    out = []
    for m in markets:
        if m["eventId"] not in ev_names:
            continue
        away, home = ev_names[m["eventId"]]
        hit = find_game(games, home, away, start=datetime.fromtimestamp(m["startsTs"] / 1000, tz=timezone.utc))
        if not hit:
            continue
        gid, g, ok = hit
        if not ok:
            continue
        m_start = datetime.fromtimestamp(m["startsTs"] / 1000, tz=timezone.utc)
        o1, o2 = m["outcomes"]
        for me, other in ((o1, o2), (o2, o1)):
            bid = other.get("bid") or {}
            if not bid.get("price"):
                continue
            p = 1 - float(bid["price"])          # buy this outcome = sell the other at its best bid
            size = 0   # Novig's bid qty units aren't documented: no size filter / display
            name = me.get("name") or ""
            if m["marketType"] == "TOTAL":
                t = re.match(r"(Over|Under)\s+([\d.]+)", name)
                if not t:
                    continue
                out.append(ex_quote(gid, g, "total", "a" if t.group(1) == "Over" else "b", float(t.group(2)), "novig", p, size, m_start))
                continue
            team = re.match(r"(\S+)(?:\s+([+-][\d.]+))?$", name)
            side = abbr.get(m["eventId"], {}).get(team.group(1)) if team else None
            if not side and m["marketType"] == "MONEY":   # tennis 'A. Campos': match on the last name
                last = name.split()[-1] if name.split() else ""
                hh, aa = name_hits(last, g["home"]), name_hits(last, g["away"])
                side = "home" if hh and not aa else "away" if aa and not hh else None
            if not side:
                continue
            s = "a" if side == "home" else "b"
            if m["marketType"] == "MONEY":
                out.append(ex_quote(gid, g, "h2h", s, None, "novig", p, size, m_start))
            elif team.group(2):
                h = float(team.group(2))
                out.append(ex_quote(gid, g, "spread", s, h if s == "a" else -h, "novig", p, size, m_start))
    return out


def kalshi_markets(series):
    ms, cursor = [], None
    for _ in range(30):
        now_ts = int(time.time())
        d = _get(f"{KALSHI}/markets", dict(series_ticker=series, status="open", limit=1000, min_close_ts=now_ts,
                                           max_close_ts=now_ts + int(3.5 * 86400), **({"cursor": cursor} if cursor else {})))
        ms += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor or not d.get("markets"):
            break
        time.sleep(0.2)
    return ms


def kalshi_date(event_ticker):
    t = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker or "")
    return datetime.strptime("".join(t.groups()), "%y%b%d").strftime("%Y-%m-%d") if t else None


SPORT_WORDS = {"pro", "professional", "basketball", "football", "hockey", "men's", "mens", "college", "nhl", "nfl",
               "soccer", "epl", "english", "premier", "league", "uefa", "champions", "laliga", "la", "liga", "serie",
               "bundesliga", "ligue", "1", "mls", "major", "tennis", "atp", "wta", "women's", "singles",
               "nba", "ncaa", "regular", "season"}


def _rules_teams(m):
    """'If Utah wins the Denver vs Utah Pro Basketball game originally scheduled ...' -> ('Denver', 'Utah')."""
    t = re.search(r"\bthe ((?:(?!\bthe\b).)+?) (?:vs\.?|at|@) (.+?) (?:game|match)\b", m.get("rules_primary") or "")
    if not t:
        return None
    b = t.group(2).split()
    while b and b[-1].lower() in SPORT_WORDS:
        b.pop()
    return (t.group(1), " ".join(b)) if b else None


def kalshi_quotes(sport, games, ml_only=False):
    out = []
    for kind, series in SPORTS[sport]["kalshi"].items():
        if ml_only and kind != "h2h":
            continue
        if kind == "spread" and "spreads" not in SPORTS[sport].get("markets", "spreads"):
            continue
        try:
            ms = kalshi_markets(series)
        except Exception as e:  # noqa: BLE001
            log(f"[{sport}] kalshi {series}: {e}")
            continue
        for m in ms:
            teams = _rules_teams(m)
            if not teams:
                continue
            hit = find_game(games, teams[0], teams[1], day=kalshi_date(m["event_ticker"]))
            if not hit:
                continue
            gid, g, first_is_home = hit
            ya, na = float(m.get("yes_ask_dollars") or 0), float(m.get("no_ask_dollars") or 0)
            ys, ns = float(m.get("yes_ask_size_fp") or 0) * ya, float(m.get("no_ask_size_fp") or m.get("yes_bid_size_fp") or 0) * na
            title = m.get("title") or ""
            if kind == "h2h":
                who = m.get("yes_sub_title") or ""
                if who.strip().lower() in ("tie", "draw"):
                    if SPORTS[sport].get("three_way") and 0 < ya < 1:
                        out.append(ex_quote(gid, g, "h2h", "d", None, "kalshi", ya, ys))
                    continue
                is_home = name_hits(who, g["home"]) and not name_hits(who, g["away"])
                is_away = name_hits(who, g["away"]) and not name_hits(who, g["home"])
                if is_home == is_away:
                    continue
                if 0 < ya < 1:
                    out.append(ex_quote(gid, g, "h2h", "a" if is_home else "b", None, "kalshi", ya, ys))
            elif kind == "spread":
                t = re.match(r"^(.*) wins by over ([\d.]+)", title)
                if not t:
                    continue
                who, n = t.group(1), float(t.group(2))
                is_home = name_hits(who, g["home"]) and not name_hits(who, g["away"])
                is_away = name_hits(who, g["away"]) and not name_hits(who, g["home"])
                if is_home == is_away:
                    continue
                yes = ("a", -n) if is_home else ("b", n)     # (side, home handicap): team -n
                no = ("b", -n) if is_home else ("a", n)      # other team +n
                for (s, h), ask, size in ((yes, ya, ys), (no, na, ns)):
                    if 0 < ask < 1:
                        out.append(ex_quote(gid, g, "spread", s, h, "kalshi", ask, size))
            else:
                n = m.get("floor_strike")
                if n is None or "over" not in title.lower():
                    continue
                for s, ask, size in (("a", ya, ys), ("b", na, ns)):
                    if 0 < ask < 1:
                        out.append(ex_quote(gid, g, "total", s, float(n), "kalshi", ask, size))
    return out


_GSPORTS = [None]


def poly_quotes(sport, games, ml_only=False):
    if _GSPORTS[0] is None:
        _GSPORTS[0] = _get(f"{GAMMA}/sports") or []
    sports = _GSPORTS[0]
    out = []
    now = datetime.now(timezone.utc)
    for code in SPORTS[sport]["poly"]:
        sp = next((s for s in sports if s.get("sport") == code), None)
        if not sp:
            continue
        evs = _get(f"{GAMMA}/events", dict(series_id=sp["series"], closed="false", limit=300,
                                           end_date_min=now.strftime("%Y-%m-%dT%H:%M:%SZ"))) or []
        for ev in evs:
            for m in ev.get("markets", []):
                typ = m.get("sportsMarketType")
                if typ not in (("moneyline",) if ml_only else ("moneyline", "spreads", "totals")) or not m.get("acceptingOrders", True):
                    continue
                try:
                    outs = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes") or []
                    start = datetime.fromisoformat((m.get("gameStartTime") or "").replace(" ", "T").replace("+00", "+00:00"))
                except (ValueError, KeyError):
                    continue
                if len(outs) != 2 or not now < start < now + timedelta(hours=HOURS_AHEAD):
                    continue
                ask, bid = m.get("bestAsk"), m.get("bestBid")
                if not ask or not bid:
                    continue
                p0, p1 = float(ask), 1 - float(bid)        # buy outcome 0 at its ask; outcome 1 = 1 - outcome 0's bid
                size = 0   # Gamma's liquidity is the whole book, not what's at the best ask
                title = ev.get("title") or ""
                if ": " in title and " vs" in title.split(": ", 1)[1]:
                    title = title.split(": ", 1)[1]      # 'Playford: Sweeny vs Delaney' (tennis tournament prefix)
                teams = re.match(r"(.+?) vs\.? (.+?)(?::|$)", title)
                if not teams:
                    continue
                hit = find_game(games, teams.group(1), teams.group(2), start=start)
                if not hit:
                    continue
                gid, g, first_is_home = hit
                if typ == "moneyline" and [o.lower() for o in outs] == ["yes", "no"]:
                    # soccer: one Yes/No market per outcome ('Will Brentford win?', '... end in a draw?')
                    git = (m.get("groupItemTitle") or m.get("question") or "")
                    if git.lower().startswith("draw") or "draw" in (m.get("question") or "").lower():
                        side = "d"
                    else:
                        h, a = name_hits(git, g["home"]), name_hits(git, g["away"])
                        side = "a" if h and not a else "b" if a and not h else None
                    if side and (side != "d" or SPORTS[sport].get("three_way")):
                        out.append(ex_quote(gid, g, "h2h", side, None, "polymarket", p0, size, start))
                    continue
                if typ == "totals":
                    line = m.get("line")
                    if line is None or outs[0].lower() != "over":
                        continue
                    for s, p in (("a", p0), ("b", p1)):
                        out.append(ex_quote(gid, g, "total", s, float(line), "polymarket", p, size, start))
                    continue
                sides = []
                for o in outs:
                    h, a = name_hits(o, g["home"]), name_hits(o, g["away"])
                    sides.append("a" if h and not a else "b" if a and not h else None)
                if None in sides or sides[0] == sides[1]:
                    continue
                if typ == "moneyline":
                    for s, p in zip(sides, (p0, p1)):
                        out.append(ex_quote(gid, g, "h2h", s, None, "polymarket", p, size, start))
                else:   # 'Spread: Spurs (-15.5)': line belongs to outcome 0
                    line = m.get("line")
                    if line is None:
                        continue
                    l0 = float(line)
                    home_h = l0 if sides[0] == "a" else -l0
                    for s, p in zip(sides, (p0, p1)):
                        out.append(ex_quote(gid, g, "spread", s, home_h, "polymarket", p, size, start))
    return out


HEALTH = {"exchange_disagree": {}}


def note_disagreements(sport, quotes_):
    by = {}
    for x in quotes_:
        if 0.1 < x["prob"] < 0.9:
            by.setdefault((x["gid"], x["kind"], x["line"], x["side"]), {})[x["book"]] = x["prob"]
    n = sum(1 for v in by.values() if len(v) >= 2 and max(v.values()) - min(v.values()) > 0.15)
    if n:
        d = HEALTH["exchange_disagree"]
        d[SPORTS[sport]["label"]] = max(d.get(SPORTS[sport]["label"], 0), n)


def exchange_quotes(sport, games, ml_only=False):
    """Pregame prices only: games past (or within minutes of) their start are dropped, by our start time or the venue's."""
    cut = datetime.now(timezone.utc) + timedelta(minutes=START_BUFFER_MIN)
    games = {k: g for k, g in games.items() if datetime.fromisoformat(g["start"]) > cut}
    res = []
    for name, fn in (("novig", novig_quotes), ("kalshi", kalshi_quotes), ("polymarket", poly_quotes)):
        try:
            res += fn(sport, games, ml_only)
        except Exception as e:  # noqa: BLE001 -- one venue down never stops the scan
            log(f"[{sport}] {name}: {type(e).__name__}: {str(e)[:120]}")
    res = [q for q in res if not q.get("start") or q["start"] > cut]
    if ml_only:
        res = [q for q in res if q["kind"] == "h2h"]
    allowed = {{"h2h": "h2h", "spreads": "spread", "totals": "total"}[m] for m in SPORTS[sport].get("markets", "h2h,spreads,totals").split(",")}
    return [q for q in res if q["kind"] in allowed]   # only the markets this sport prices


def ml_mid(quotes_, gid):
    """Exchange consensus P(home) for a game from free moneyline quotes (average of each venue's two asks)."""
    by = {}
    for q in quotes_:
        if q["gid"] == gid and q["kind"] == "h2h":
            by.setdefault(q["book"], {})[q["side"]] = q["prob"]
    vals = [v["a"] / (v["a"] + v["b"]) for v in by.values() if "a" in v and "b" in v and 0.8 < v["a"] + v["b"] < 1.25]
    return sum(vals) / len(vals) if vals else None


# ---------------- alerts ----------------
EXCHANGES = {"novig", "kalshi", "polymarket", "prophetx"}


def webhook():
    return os.environ.get("SCAN_DISCORD_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL")


BUTTONS = [{"type": 1, "components": [
    {"type": 2, "style": 3, "label": "Bet it", "custom_id": "bet"},
    {"type": 2, "style": 2, "label": "Line moved", "custom_id": "moved"},
    {"type": 2, "style": 2, "label": "Pass", "custom_id": "pass"}]}]
BOT_MSGS = set()          # alert messages the bot posted (it edits those; the webhook edits its own)
BOT_CAN_POST = [True]


def discord_send(text, edit_id=None, buttons=True):
    """Post one alert (returns its message id), or edit an earlier one in place (no new notification).
    With the bot: posted by the bot with Bet it / Pass buttons. Without it (or no Send Messages permission): webhook."""
    if DRY:
        print("   [edit]" if edit_id else "   [alert]", text.replace("\n", " | "))
        return edit_id or f"dry-{abs(hash(text)) % 10**8}"
    if not POST or not webhook():   # public log: never print prices
        log("alert not posted (posting off or no webhook)")
        return None
    try:
        if bot_on() and (edit_id in BOT_MSGS if edit_id else BOT_CAN_POST[0]):
            if edit_id:
                bot("PATCH", f"/channels/{channel_id()}/messages/{edit_id}", json={"content": text})
                time.sleep(0.3)
                return edit_id
            try:
                m = bot("POST", f"/channels/{channel_id()}/messages",
                        json={"content": text, **({"components": BUTTONS} if buttons else {})})
                if buttons:
                    BOT_MSGS.add(m["id"])
                time.sleep(0.3)
                return m["id"]
            except requests.HTTPError as e:
                if e.response.status_code != 403:
                    raise
                BOT_CAN_POST[0] = False
                log("bot can't send messages in the alert channel (give it Send Messages) -- using the webhook")
        url = webhook()
        if edit_id:
            r = requests.patch(f"{url}/messages/{edit_id}", json={"content": text}, timeout=20)
        else:
            r = requests.post(url, params={"wait": "true"}, json={"content": text}, timeout=20)
        time.sleep(0.6)   # webhooks allow ~5 messages per 2 seconds
        r.raise_for_status()
        return r.json().get("id") if not edit_id else edit_id
    except Exception as e:  # noqa: BLE001 -- a Discord hiccup never stops the scan
        log(f"discord {'edit' if edit_id else 'post'} failed: {type(e).__name__} {getattr(e, 'response', None) and e.response.status_code}")
        return None


BOT = os.environ.get("DISCORD_BOT_TOKEN", "")
DAPI = "https://discord.com/api/v10"
TAKE, PASS = "\u2705", "\u274c"     # ✅ ❌
_CHANNEL = [None]


def channel_id():
    """The alert channel, from the webhook itself (no extra setting needed)."""
    if _CHANNEL[0] is None and webhook() and not DRY:
        try:
            _CHANNEL[0] = requests.get(webhook(), timeout=20).json().get("channel_id") or ""
        except Exception:  # noqa: BLE001
            _CHANNEL[0] = ""
    return _CHANNEL[0]


def bot_on():
    return bool(BOT) and POST and not DRY and bool(channel_id())


def bot(method, path, **kw):
    r = requests.request(method, f"{DAPI}{path}", headers={"Authorization": f"Bot {BOT}"}, timeout=20, **kw)
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 1)) + 0.2)
        r = requests.request(method, f"{DAPI}{path}", headers={"Authorization": f"Bot {BOT}"}, timeout=20, **kw)
    r.raise_for_status()
    return r.json() if r.content else None


def add_buttons(mid):
    """✅ / ❌ under a new alert, so marking a bet is one tap."""
    if not (mid and bot_on()) or mid in BOT_MSGS:   # bot messages have buttons instead
        return
    for e in (TAKE, PASS):
        try:
            bot("PUT", f"/channels/{channel_id()}/messages/{mid}/reactions/{requests.utils.quote(e)}/@me")
            time.sleep(0.3)
        except Exception as ex:  # noqa: BLE001
            log(f"reaction add failed: {type(ex).__name__}")
            return


def parse_reply(text):
    """What you actually bet, from a reply to an alert: '10 @ 46.5' ($10 at 46.5¢), '$25 -110', '$10', '12 at 52c'.
    -> (stake or None, American odds or None, contract price 0-1 or None)."""
    t = (text or "").lower().replace(",", "")
    odds = price = None
    m = re.search(r"(?<![\d.$])([+-]\d{3,5})(?![\d.])", t)
    if m:
        odds = int(m.group(1))
        t = t[:m.start()] + " " + t[m.end():]
    else:
        m = (re.search(r"(?:@|\bat\b)\s*(\d{1,2}(?:\.\d+)?)\s*(?:¢|c\b|cents?\b)?", t)
             or re.search(r"(\d{1,2}(?:\.\d+)?)\s*(?:¢|c\b|cents?\b)", t))
        if m:
            price = float(m.group(1)) / 100
            odds = american(price) if 0 < price < 1 else None
            t = t[:m.start()] + " " + t[m.end():]
    m = re.search(r"\$\s*(\d+(?:\.\d+)?)", t) or re.search(r"(?<![\d.])(\d+(?:\.\d+)?)", t)
    stake = float(m.group(1)) if m else None
    return stake, odds, price


def taps_from_text(content, x):
    """Read the tap line the scanner wrote on an alert ('✅ You bet $10 at 45.5¢ (+120)', '❌ Missed -- line moved',
    '❌ Passed') back into record fields."""
    m = re.search(r"✅ \*\*You bet\*\* \$([\d.]+)(?: on [^@]*?)? at (?:([\d.]+)¢ \(([+-]\d+)\)|([+-]\d+) \()", content)
    if m:
        stake = float(m.group(1))
        odds = int(m.group(3) or m.group(4))
        pr = float(m.group(2)) / 100 if m.group(2) else None
        return dict(taken=True, stake_taken=stake, taken_odds=odds, taken_price=pr, status_final=True, restored=True)
    if "❌ **Missed -- line moved**" in content:
        return dict(taken=False, pass_reason="line moved", status_final=True, restored=True)
    if "❌ **Passed**" in content:
        return dict(taken=False, pass_reason="didn't want it", status_final=True, restored=True)
    return None


def read_reactions(seen, now):
    """Mark alerts taken (✅) or passed (❌) from the owner's reactions; a reply like '$20' sets the stake taken."""
    if not bot_on():
        return 0
    live = {}
    for x in seen.values():
        if x.get("alerted") and not x.get("superseded") and x.get("msg_id"):
            live[x["msg_id"]] = x
    if not live:
        return 0
    oldest = min(x.get("alerted_at") or "" for x in live.values())
    msgs, before = [], None
    for _ in range(5):
        try:
            page = bot("GET", f"/channels/{channel_id()}/messages", params=dict(limit=100, **({"before": before} if before else {})))
        except Exception as ex:  # noqa: BLE001
            log(f"reaction read failed: {type(ex).__name__}")
            return 0
        msgs += page or []
        if not page or len(page) < 100 or (page[-1].get("timestamp") or "") < oldest:
            break
        before = page[-1]["id"]
    changed = 0
    stakes = {}
    for m in sorted(msgs, key=lambda m: m.get("timestamp") or ""):   # replies: '10 @ 46.5', '$25 -110', '$10'
        ref = (m.get("message_reference") or {}).get("message_id")
        if ref in live and not (m.get("author") or {}).get("bot"):
            got = parse_reply(m.get("content"))
            if got[0] is not None or got[1] is not None:
                stakes[ref] = got           # the latest reply wins
    for m in msgs:
        x = live.get(m["id"])
        if not x:
            continue
        if m["id"] in BOT_MSGS or x.get("via") == "bot":
            # button alerts: the message itself shows what you tapped -- restore the record from it if it was lost
            if x.get("taken") is None:
                got = taps_from_text(m.get("content") or "", x)
                if got:
                    x.update(got)
                    fs_set(f"scan_plays/{x.get('_id') or play_id(x, x['event_id'])}", {k: v for k, v in x.items() if k != "_id"})
                    changed += 1
            continue
        if x.get("tapped_at"):
            continue   # never let reactions override a button tap
        mine = {r["emoji"].get("name"): r["count"] - (1 if r.get("me") else 0) for r in m.get("reactions") or []}
        taken = True if mine.get(TAKE, 0) > 0 else False if mine.get(PASS, 0) > 0 else None
        stake, odds_t, price_t = stakes.get(m["id"], (None, None, None))
        if (taken != x.get("taken") or (stake is not None and stake != x.get("stake_taken"))
                or (odds_t is not None and odds_t != x.get("taken_odds"))):
            x["taken"] = taken
            if stake is not None or odds_t is not None:
                x["taken"] = True if taken is None else taken   # a reply means you bet it
            if stake is not None:
                x["stake_taken"] = stake
            if odds_t is not None:
                x["taken_odds"], x["taken_price"] = odds_t, price_t
            fs_set(f"scan_plays/{x.get('_id') or play_id(x, x['event_id'])}", {k: v for k, v in x.items() if k != "_id"})
            changed += 1
    return changed


# ---- Bet it / Pass buttons: the bot listens on Discord's gateway while the scan runs ----
TAPS = queue.Queue()      # (message id, 'bet' | 'pass', stake, odds, contract price) for the main loop
MSG_DOC = {}              # message id -> its live play (refreshed by the main loop every cycle)


def parse_price(v):
    """'46.5' / '46.5c' / '.465' -> cents; '-110' / '+115' / '115' -> American. -> (American odds, price 0-1 or None)."""
    t = (v or "").strip().lower().replace("¢", "").replace("c", "").replace(" ", "")
    if re.fullmatch(r"[+-]\d{3,5}", t):
        return int(t), None
    x = float(t)
    if 0 < x < 1:
        return american(x), x
    if 1 <= x < 100:
        return american(x / 100), x / 100
    if abs(x) >= 100:
        return int(x), None
    raise ValueError(v)


def _line_str(x):
    """The alert's line as you'd read it on the bet: '+3.5' for your side of a spread, '144.5' for a total."""
    if x.get("line") is None:
        return ""
    if x["kind"] == "total":
        return f"{x['line']:g}"
    return f"{(x['line'] if x['side'] == 'a' else -x['line']):+g}"


def _default_price(x):
    if x.get("odds") is None:
        return ""
    if x["book"] in EXCHANGES:
        return _pct(x.get("price_prob") or prob(x["odds"]))
    return f"{x['odds']:+d}"


def _answer(it):
    reply = lambda body: requests.post(f"{DAPI}/interactions/{it['id']}/{it['token']}/callback", json=body, timeout=10)
    note = lambda msg: reply({"type": 4, "data": {"content": msg, "flags": 64}})   # only you see it
    try:
        if it["type"] == 3:   # button
            mid = it["message"]["id"]
            x = MSG_DOC.get(mid) or {"book": "", "odds": None, "kind": "", "stake_usd": ""}
            if it["data"]["custom_id"] == "pass":
                TAPS.put((mid, "pass", None, None, None))
                return note("Passed ❌")
            if it["data"]["custom_id"] == "moved":
                x_ = MSG_DOC.get(mid) or {}
                comps = [{"type": 1, "components": [{"type": 4, "custom_id": "price", "style": 1, "required": False,
                                                     "max_length": 10, "label": "Best price now (optional)",
                                                     "placeholder": "-257 or 52.5"}]}]
                if x_.get("line") is not None:   # spreads / totals: the number may have moved instead of the price
                    comps.append({"type": 1, "components": [{"type": 4, "custom_id": "line", "style": 1, "required": False,
                                                             "max_length": 8, "label": "Line now (blank = same as alert)",
                                                             "placeholder": _line_str(x_)}]})
                return reply({"type": 9, "data": {"custom_id": f"moved:{mid}", "title": "Line moved", "components": comps}})
            stake = x.get("stake_taken") or x.get("stake_usd") or ""
            return reply({"type": 9, "data": {"custom_id": f"bet:{mid}", "title": "Log your bet"[:45], "components": [
                {"type": 1, "components": [{"type": 4, "custom_id": "amount", "label": "Amount ($)", "style": 1,
                                            "value": f"{stake:g}" if stake != "" else "", "required": True, "max_length": 10}]},
                {"type": 1, "components": [{"type": 4, "custom_id": "price", "style": 1, "required": True, "max_length": 10,
                                            "label": ("Price in cents (e.g. 46.5)" if x["book"] in EXCHANGES
                                                      else "Odds (e.g. -115)"), "value": _default_price(x)}]}]}})
        if it["type"] == 5 and it["data"]["custom_id"].startswith("moved:"):
            mid = it["data"]["custom_id"].split(":", 1)[1]
            vals = {c["components"][0]["custom_id"]: c["components"][0].get("value", "").strip()
                    for c in it["data"]["components"]}
            odds = pr = line_now = None
            try:
                if vals.get("price"):
                    odds, pr = parse_price(vals["price"])
                if vals.get("line"):
                    line_now = float(vals["line"].replace(" ", ""))
            except ValueError:
                return note("Couldn't read that -- price like -110 or 52.5, line like 145.5 or +3.5. Tap Line moved again "
                            "(blank boxes are fine).")
            TAPS.put((mid, "moved", line_now, odds, pr))   # (the stake slot carries the new line for 'moved')
            x = MSG_DOC.get(mid) or {"book": ""}
            bits = ([f"line now {line_now:g}"] if line_now is not None else []) + \
                   ([f"price now {price_str(odds, x['book'], pr)}"] if odds is not None else [])
            return note("Noted: missed, line moved" + (f" ({', '.join(bits)})" if bits else ""))
        if it["type"] == 5:   # form submitted
            mid = it["data"]["custom_id"].split(":", 1)[1]
            vals = {c["components"][0]["custom_id"]: c["components"][0].get("value", "") for c in it["data"]["components"]}
            try:
                stake = float(vals["amount"].replace("$", "").strip())
                odds, pr = parse_price(vals["price"])
            except (ValueError, KeyError):
                return note("Couldn't read that -- amount like 10, price like 46.5 (cents) or -110 (odds). Tap Bet it again.")
            TAPS.put((mid, "bet", stake, odds, pr))
            x = MSG_DOC.get(mid) or {"book": "", "odds": odds}
            return note(f"Logged ✅ ${stake:g} at {price_str(odds, x['book'], pr)}")
    except Exception as e:  # noqa: BLE001
        log(f"button answer failed: {type(e).__name__}")


def gateway():
    """Discord gateway connection (thread): receives button taps and form submits. Reconnects on drops."""
    import websocket   # websocket-client
    while True:
        ws, alive = None, [True]
        try:
            ws = websocket.create_connection("wss://gateway.discord.gg/?v=10&encoding=json", timeout=60)
            hb = json.loads(ws.recv())["d"]["heartbeat_interval"] / 1000
            seq = [None]

            def beat():
                while alive[0]:
                    time.sleep(hb)
                    if alive[0]:
                        ws.send(json.dumps({"op": 1, "d": seq[0]}))
            threading.Thread(target=beat, daemon=True).start()
            ws.send(json.dumps({"op": 2, "d": {"token": BOT, "intents": 0,
                                               "properties": {"os": "linux", "browser": "scan", "device": "scan"}}}))
            ws.settimeout(hb * 2 + 5)
            while True:
                msg = json.loads(ws.recv())
                if msg.get("s") is not None:
                    seq[0] = msg["s"]
                if msg["op"] == 0 and msg.get("t") == "READY":
                    log("gateway: listening for Bet it / Pass taps")
                if msg["op"] == 0 and msg.get("t") == "INTERACTION_CREATE":
                    threading.Thread(target=_answer, args=(msg["d"],), daemon=True).start()
                elif msg["op"] == 1:
                    ws.send(json.dumps({"op": 1, "d": seq[0]}))
                elif msg["op"] in (7, 9):   # reconnect / invalid session
                    break
        except Exception as e:  # noqa: BLE001
            log(f"gateway: {type(e).__name__} -- reconnecting")
        finally:
            alive[0] = False
            try:
                ws and ws.close()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(5)


def apply_taps(seen):
    """Main loop: record button taps / forms on the play, and show it on the alert."""
    n, later = 0, []
    while True:
        try:
            item = TAPS.get_nowait()
        except queue.Empty:
            break
        mid, what, stake, odds, pr = item[:5]
        tapped = item[5] if len(item) > 5 else time.time()
        x = MSG_DOC.get(mid)
        if not x:   # the scan doesn't know this alert yet (e.g. just restarted): keep the tap and retry for 30 min
            if time.time() - tapped < 1800:
                later.append((mid, what, stake, odds, pr, tapped))
            continue
        if what == "pass":
            x.update(taken=False, pass_reason="didn't want it")
            line = "❌ **Passed**"
        elif what == "moved":
            line_now = stake   # for 'moved' the stake slot carries the line you saw (or None = same as the alert)
            x.update(taken=False, pass_reason="line moved", moved_odds=odds, moved_price=pr, moved_line_seen=line_now,
                     moved_at=datetime.now(timezone.utc).isoformat())
            bits = []
            if line_now is not None:
                bits.append(("Over " if x["side"] == "a" else "Under ") + f"{line_now:g}" if x["kind"] == "total" else f"{line_now:+g}")
            if odds is not None:
                bits.append(price_str(odds, x["book"], pr))
            line = "❌ **Missed -- line moved**" + (f" (now {' '.join(bits)})" if bits else "")
        else:
            x.update(taken=True, stake_taken=stake, taken_odds=odds, taken_price=pr)
            line = f"✅ **You bet** ${stake:g} at {price_str(odds, x['book'], pr)}"
        x.update(status_line=line, status_final=True, tapped_at=datetime.now(timezone.utc).isoformat())
        discord_send((x.get("msg_text") or alert_text(x, x)) + "\n" + line, edit_id=mid)
        fs_set(f"scan_plays/{x.get('_id') or play_id(x, x['event_id'])}", {k: v for k, v in x.items() if k != "_id"})
        n += 1
    for it_ in later:
        TAPS.put(it_)
    return n


def open_exposure(seen, now):
    """Dollars in bets not yet settled. With reactions: ✅ bets plus alerts under an hour old with no answer yet.
    Without: every alert counts (the scanner can't know which ones you took)."""
    tot = 0.0
    for x in seen.values():
        if not x.get("alerted") or x.get("superseded") or x.get("result") or x.get("taken") is False:
            continue
        if datetime.fromisoformat(x["start"]) < now - timedelta(hours=4):
            continue
        if bot_on() and x.get("taken") is None:
            if (now - datetime.fromisoformat(x.get("alerted_at") or now.isoformat())).total_seconds() > 3600:
                continue
        tot += x.get("stake_taken") or x.get("stake_usd") or 0
    return tot


BOOK_NAMES = {"novig": "Novig", "prophetx": "ProphetX", "kalshi": "Kalshi", "polymarket": "Polymarket",
              "fanduel": "FanDuel", "betrivers": "BetRivers", "ballybet": "Bally Bet", "betmgm": "BetMGM",
              "betonlineag": "BetOnline", "espnbet": "theScore Bet",
              "williamhill_us": "Caesars"}


def _pct(p):
    return f"{p * 100:.1f}".rstrip("0").rstrip(".")


def price_str(odds, book, p=None):
    """Exchanges: '46¢ (+117)'; sportsbooks: '+117 (46.1%)'. p = the exchange's own contract price if known."""
    p = prob(odds) if p is None else p
    return f"{_pct(p)}¢ ({odds:+d})" if book in EXCHANGES else f"{odds:+d} ({_pct(p)}%)"


def alert_text(p, g, first=None):
    """first = the play doc that was originally posted, when this is an in-place update."""
    t = datetime.fromisoformat(g["start"]).astimezone(ET).strftime("%a %-I:%M %p")
    bk = BOOK_NAMES.get(p["book"], p["book"])
    good = ""
    if p.get("good_to") is not None:
        good = f" · good to {price_str(p['good_to'], p['book'])}"
    size = f" · ${p['size_usd']:,} avail" if p.get("size_usd") else ""
    at_ = " vs " if SPORTS[p["sport"]].get("odds_prefix") else " @ "   # tennis has no home side
    small = " · 🔹 small edge" if p.get("ev", 1) < BAR else ""
    txt = (f"🔎 **{SPORTS[p['sport']]['label']}** {g['away']}{at_}{g['home']} · {t} ET{small}\n"
           f"**{p['label']} · {price_str(p['odds'], p['book'], p.get('price_prob'))} @ {bk}**\n"
           f"Fair {_pct(p['p_fair'])}% ({p['fair_odds']:+d}) · EV {p['ev']:+.1%} · bet ${p['stake_usd']} to win ${p.get('to_win') or p['stake_usd'] * payout(p['odds'], p['book'], p['sport']):,.0f}{good}{size}")
    if first:
        was = price_str(first["odds"], first["book"], first.get("price_prob"))
        wbk = BOOK_NAMES.get(first["book"], first["book"])
        wl = "" if first["line"] == p["line"] else f" {first['label']}"
        at = datetime.now(ET).strftime("%-I:%M %p")
        txt = f"🔄 **Better number at {at}** -- first posted{wl} at {was} @ {wbk}\n" + txt
    return txt


def region(p):
    """Where a play wins, as (axis, direction, threshold): margin = home minus away, total = combined score.
    home ML: margin > 0; home spread h: margin > -h; away mirrors with '<'; over L: total > L; under: total < L."""
    if p["kind"] == "total":
        return ("total", ">" if p["side"] == "a" else "<", p["line"])
    if p["kind"] == "h2h" and (p["side"] == "d" or SPORTS.get(p.get("sport"), {}).get("three_way")):
        return None   # home / away / draw: three separate outcomes, each priced on its own
    t = 0.0 if p["kind"] == "h2h" else -p["line"]
    return ("margin", ">" if p["side"] == "a" else "<", t)


def _bounds(axis, lo, hi, g):
    """Words for the open interval lo < x < hi."""
    if axis == "total":
        return f"the total lands between {lo:g} and {hi:g}"
    if lo >= 0:
        return (f"{g['home']} wins by less than {hi:g}" if lo == 0
                else f"{g['home']} wins by more than {lo:g} but less than {hi:g}")
    if hi <= 0:
        return (f"{g['away']} wins by less than {-lo:g}" if hi == 0
                else f"{g['away']} wins by more than {-hi:g} but less than {-lo:g}")
    return f"{g['away']} wins by less than {-lo:g} or {g['home']} wins by less than {hi:g}"


def _odds_held(x):
    return x.get("taken_odds") or x.get("odds_alerted") or x["odds"]


def middle_math(over, under, axis, lo, hi):
    """(hit chance or None, break-even chance) for holding both sides. Break-even = the pair's combined implied
    chance minus 1 (what the vig costs when only one side wins). Hit chance: scores are whole numbers, each one
    strictly between the lines counted from the sport's normal spread/total SD centered on the middle."""
    d_o = 1 + payout(_odds_held(over), over["book"], over["sport"])
    d_u = 1 + payout(_odds_held(under), under["book"], under["sport"])
    be = max(0.0, 1 / d_o + 1 / d_u - 1)
    sd = (SPORTS.get(over["sport"], {}).get("sd") or {}).get("total" if axis == "total" else "spread")
    if not sd:
        return None, be
    mu = (lo + hi) / 2
    ks = [k for k in range(math.floor(lo) + 1, math.ceil(hi)) if not (axis == "margin" and k == 0)]
    return sum(N.pdf((k - mu) / sd) / sd for k in ks), be


def middle_words(text, mm):
    hit, be = mm
    if be <= 0:
        return f"🔥 **Free middle**: {text}. The pair can't lose -- worst case you break even or better."
    odds = (f"Middle hits ~{hit:.1%}, needs {be:.1%} to pay for the vig on its own" if hit is not None
            else f"Middle needs {be:.1%} to pay for the vig on its own")
    return f"🎯 **Middle**: {text}. {odds} -- this bet stands on its own EV; the middle is a bonus."


def relation(p, q, g):
    """p and q on opposite sides of the same game: ('middle' | 'arb' | 'conflict', text) or None if not opposed."""
    if region(p) is None or region(q) is None:
        return None
    ax1, d1, t1 = region(p)
    ax2, d2, t2 = region(q)
    if ax1 != ax2 or d1 == d2:
        return None
    over, under = (p, q) if d1 == ">" else (q, p)
    lo, hi = region(over)[2], region(under)[2]
    if lo < hi:
        return "middle", (f"both win if {_bounds(ax1, lo, hi, g)}", middle_math(over, under, ax1, lo, hi))
    if lo == hi:
        d_o = 1 + payout(over["odds"], over["book"], over["sport"])
        d_u = 1 + payout(under["odds"], under["book"], under["sport"])
        if 1 / d_o + 1 / d_u < 1:
            k = 100 / (1 / d_o + 1 / d_u)   # total return per $100 split across both
            return "arb", (f"bet ${k / d_o:,.0f} on {over['label']} and ${k / d_u:,.0f} on {under['label']} "
                           f"(per $100 total) and you get ${k:,.0f} back either way")
        return "conflict", "exact opposites"
    return "conflict", f"both lose if {_bounds(ax1, hi, lo, g)}"


def _note_for(kind, text, other, better):
    bk = BOOK_NAMES.get(other["book"], other["book"])
    ref = f"{other['label']} {price_str(other.get('odds_alerted') or other['odds'], other['book'], other.get('price_prob'))} @ {bk} (EV {other['ev']:+.1%})"
    if kind == "middle":
        return middle_words(f"with {ref}, {text[0]}", text[1])
    if kind == "arb":
        return f"💰 **Arb** with {ref}: {text}"
    return (f"⚠️ **Opposite side** of {ref}: {text}. "
            + ("This one is the better value -- if you haven't bet the other, take this one only."
               if better else "The other one is the better value -- take only one."))


def _taken_note(kind, text, o, p):
    """Note on a new play when the owner already bet the other side (o, marked ✅)."""
    bk = BOOK_NAMES.get(o["book"], o["book"])
    stake = o.get("stake_taken") or o.get("stake_usd") or 0
    ref = f"${stake:,.0f} on {o['label']} {price_str(o.get('odds_alerted') or o['odds'], o['book'], o.get('price_prob'))} @ {bk}"
    if kind == "middle":
        return middle_words(f"you already bet {ref}; {text[0]}", text[1])
    d_o = 1 + payout(o.get("odds_alerted") or o["odds"], o["book"], o["sport"])
    d_p = 1 + payout(p["odds"], p["book"], p["sport"])
    hedge = stake * d_o / d_p
    return (f"💰 **Lock it in:** you already bet {ref}. Bet ${hedge:,.0f} here and you get "
            f"${stake * d_o:,.2f} back either way ({'+' if stake * d_o - stake - hedge >= 0 else '-'}${abs(stake * d_o - stake - hedge):,.2f} locked).")


def record(found, seen, now):
    """Save new plays. A game/market/side is posted once; a better number later (same line 10+ cents better, or
    another line with 2%+ more EV) edits that same Discord message instead of posting again."""
    best = {}
    for gid, g, p, size in found:   # one play per game / market / side: the best EV across books and alt lines
        k = (p["sport"], gid, p["kind"], p["side"], bool(p.get("test")))   # test plays never crowd out real ones
        if k not in best or p["ev"] > best[k][2]["ev"]:
            best[k] = (gid, g, p, size)
    posted = 0
    for gid, g, p, size in sorted(best.values(), key=lambda b: -b[2]["ev"]):
        pid = play_id(p, gid)
        prev = seen.get(pid)
        fam = f"{p['sport']}_{gid}_{p['kind']}_{p['side']}"
        sib = sorted((x for x in seen.values() if x.get("alerted") and not x.get("superseded")
                      and f"{x['sport']}_{x['event_id']}_{x['kind']}_{x['side']}" == fam),
                     key=lambda x: x.get("alerted_at") or "")
        extra = {"size_usd": round(size)} if size else {}
        base = dict(**p, event_id=gid, home=g["home"], away=g["away"], start=g["start"], fair_at=g["at"],
                    **({"okey": g["okey"]} if g.get("okey") else {}), **extra)
        try:
            base["fv"] = fair_variants(p["sport"], g, p["kind"], p["side"], p["line"], p["book"], now)
        except Exception:  # noqa: BLE001 -- the test never blocks an alert
            pass
        if p.get("test"):   # 1%-to-bar play in a sharp market: logged and graded, never posted
            if not prev:
                d_ = dict(base, found=now.isoformat(), alerted=False, shadow=True, test=True)
                fs_set(f"scan_plays/{pid}", d_)
                seen[pid] = d_
            continue
        hrs_out = (datetime.fromisoformat(g["start"]) - now).total_seconds() / 3600
        window = float(os.environ.get(f"SCAN_POST_WITHIN_{p['sport'].upper()}", POST_WITHIN_H))
        if hrs_out > window and not sib:   # too far out to trust yet: logged and graded (timing test)
            if not prev:
                d_ = dict(base, found=now.isoformat(), alerted=False, shadow=True, early=True)
                fs_set(f"scan_plays/{pid}", d_)
                seen[pid] = d_
            continue
        if not SPORTS[p["sport"]].get("alerts", True):   # log-only league: priced and graded, never posted
            if not prev:
                d_ = dict(base, found=now.isoformat(), alerted=False, shadow=True)
                fs_set(f"scan_plays/{pid}", d_)
                seen[pid] = d_
            continue
        # plays already posted in this game: other side (middle / arb / conflict) or the same team another way
        notes, edits, skip, rel_tags = [], [], False, []
        for o in [x for x in seen.values() if x.get("alerted") and not x.get("superseded") and x["event_id"] == gid
                  and x["sport"] == p["sport"] and x["kind"] in ("h2h", "spread", "total") and x.get("taken") is not False]:
            r1, r2 = region(p), region(o)
            if r1 is None or r2 is None:
                continue
            if o.get("taken"):   # you already bet o: only a middle or a lock is worth a message
                if r1[:2] == r2[:2] and (o["kind"], o["side"]) != (p["kind"], p["side"]):
                    skip = True          # same team another way: that's doubling up
                    break
                rel = relation(p, o, g)
                if rel and rel[0] in ("middle", "arb"):
                    notes.append(_taken_note(rel[0], rel[1], o, p))
                    rel_tags.append(rel[0] + "_with_bet")
                elif rel:
                    skip = True          # the other side of a bet you hold, with a both-lose gap: don't send
                    break
                continue
            if r1[:2] == r2[:2] and (o["kind"], o["side"]) != (p["kind"], p["side"]):
                # e.g. Nebraska ML and Nebraska -10.5: one bet on Nebraska, not two
                if p["ev"] <= o["ev"]:
                    skip = True
                    break
                rel_tags.append("same_side")
                notes.append(f"↪️ **Same side** as {o['label']} @ {BOOK_NAMES.get(o['book'], o['book'])} (EV {o['ev']:+.1%}) "
                             f"-- that's one bet on the same result; this is the better way. If you took that one, skip this.")
                edits.append((o, f"↪️ A better way to bet this side was posted: {p['label']} "
                                 f"{price_str(p['odds'], p['book'], p.get('price_prob'))} @ {BOOK_NAMES.get(p['book'], p['book'])} "
                                 f"(EV {p['ev']:+.1%}). Take only one."))
                continue
            rel = relation(p, o, g)
            if not rel:
                continue
            kind, text = rel
            if kind == "conflict" and p["ev"] <= o["ev"]:
                skip = True   # the play already posted is the better value: don't post the weaker opposite
                break
            notes.append(_note_for(kind, text, o, better=True))
            rel_tags.append(kind)
            edits.append((o, _note_for(kind, text, {**p, **extra}, better=False) if kind == "conflict"
                          else _note_for(kind, text, {**p, **extra}, better=True)))
        if skip:
            if not prev:
                fs_set(f"scan_plays/{pid}", dict(base, found=now.isoformat(), alerted=False, conflict=True))
                seen[pid] = dict(base, found=now.isoformat(), alerted=False, conflict=True)
            continue
        note = ("\n" + "\n".join(notes)) if notes else ""
        if EXPOSURE_CAP and not sib and p["ev"] < EXPOSURE_OVERRIDE_EV:
            exp = open_exposure(seen, now)
            if exp + p["stake_usd"] > EXPOSURE_CAP * BANKROLL:
                if not prev:
                    d_ = dict(base, found=now.isoformat(), alerted=False, skipped="exposure cap")
                    fs_set(f"scan_plays/{pid}", d_)
                    seen[pid] = d_
                continue
        fam_key = f"{p['sport']}_{gid}_{p['kind']}_{p['side']}"

        def tell_others():   # only once this play is really posted (or re-posted at a better number)
            for o, onote in edits:
                if not o.get("msg_id"):
                    continue
                first = seen.get(o.get("first_id")) if o.get("updated") else None
                o.setdefault("msg_base", o.get("msg_text") or alert_text(o, o, first=first))
                notes = dict(o.get("notes") or {})
                if notes.get(fam_key) == onote:
                    continue          # same note already there: no edit
                notes[fam_key] = onote    # one note per other play: a newer one replaces it
                o["notes"] = notes
                body = o["msg_base"] + "".join("\n" + n for n in notes.values())
                discord_send(body + status_suffix(o), edit_id=o["msg_id"])
                o["msg_text"] = body
                fs_set(f"scan_plays/{o.get('_id') or play_id(o, o['event_id'])}", {k: v for k, v in o.items() if k != "_id"})
        if not sib:
            tell_others()
            text = alert_text({**p, **extra}, g) + note
            mid = discord_send(text)
            add_buttons(mid)
            doc = dict(base, found=prev["found"] if prev else now.isoformat(), alerted=True, odds_alerted=p["odds"],
                       alerted_at=now.isoformat(), msg_id=mid, first_id=pid, msg_text=text, msg_base=text,
                       via="bot" if mid in BOT_MSGS else "webhook", **({"rel": sorted(set(rel_tags))} if rel_tags else {}))
            fs_set(f"scan_plays/{pid}", doc)
            seen[pid] = doc
            if mid:
                MSG_DOC[mid] = doc   # buttons work on it right away, not after the cycle ends
            posted += 1
            continue
        last = sib[-1]
        if last.get("taken"):   # already bet: a better number later is just noise
            if not prev:
                d_ = dict(base, found=now.isoformat(), alerted=False, skipped="already bet")
                fs_set(f"scan_plays/{pid}", d_)
                seen[pid] = d_
            continue
        same_line = last["line"] == p["line"]
        better = (same_line and payout(p["odds"], "x", p["sport"]) * 100
                  >= payout(last.get("odds_alerted") or last["odds"], "x", p["sport"]) * 100 + REPOST_CENTS) \
            or (not same_line and p["ev"] >= last["ev"] + 0.02)
        if not better:
            if not prev:
                fs_set(f"scan_plays/{pid}", dict(base, found=now.isoformat(), alerted=False))
                seen[pid] = dict(base, found=now.isoformat(), alerted=False)
            continue
        tell_others()
        first = seen.get(last.get("first_id")) or last
        text = alert_text({**p, **extra}, g, first=first) + note
        mid = discord_send(text, edit_id=last.get("msg_id"))
        if mid is None and last.get("msg_id"):
            mid = last["msg_id"]
        # the older doc stays for the record but is graded as superseded; the bettor took one of them
        fs_set(f"scan_plays/{last['_id'] if last.get('_id') else play_id(last, last['event_id'])}",
               {**{k: v for k, v in last.items() if k != "_id"}, "superseded": True})
        last["superseded"] = True
        doc = dict(base, found=prev["found"] if prev else now.isoformat(), alerted=True, odds_alerted=p["odds"],
                   alerted_at=now.isoformat(), msg_id=mid, first_id=last.get("first_id") or pid, updated=True,
                   msg_text=text, msg_base=text, taken=last.get("taken"), stake_taken=last.get("stake_taken"),
                   via=last.get("via"), **({"rel": sorted(set(rel_tags))} if rel_tags else {}))
        fs_set(f"scan_plays/{pid}", doc)
        seen[pid] = doc
        if mid:
            MSG_DOC[mid] = doc
        posted += 1
    return posted


# ---------------- live status on posted alerts ----------------
FREE_FEED = {"novig", "kalshi", "polymarket"}


def status_suffix(x):
    return ("\n" + x["status_line"]) if x.get("status_line") else ""


def _ev_now(sport, g, kind, side, line, book, odds):
    views = (g.get("views") or {}).get(kind)
    pw = fair_side(sport, kind, views, line, side, without=book) if views else None
    if pw is None:
        return None
    return pw * (1 + payout(odds, book, sport)) - 1


def live_status(sport, games, quotes_, seen, now, full):
    """Re-check every posted alert's own price (same book / line / side) and keep a status line on its message:
    🟢 still good · 🟡 price there, edge under the bar · 🔴 gone · ⏱️ game started. Records when each alert died."""
    free = {}
    for x in quotes_:
        k = (x["gid"], x["kind"], x["side"], x["line"], x["book"])
        if k not in free or payout(x["odds"], x["book"], sport) > payout(free[k][0], x["book"], sport):
            free[k] = (x["odds"], x.get("prob"))
    edits = 0
    for x in list(seen.values()):
        if (x.get("sport") != sport or not x.get("alerted") or x.get("superseded") or not x.get("msg_id")
                or x.get("status_final") or x.get("taken") is not None):   # bet or passed: leave it alone
            continue
        g = games.get(x["event_id"])
        if not g:
            continue
        key = (x["event_id"], x["kind"], x["side"], x["line"], x["book"])
        started = datetime.fromisoformat(x["start"]) <= now
        if x["book"] in FREE_FEED:
            if x["kind"] != "h2h" and not full and not started:
                continue            # spreads / totals are re-read every 3rd cycle
            cur = free.get(key)
            odds, pp = (cur if cur else (None, None))
            at, src = now, "live"
        else:
            odds, pp = BOOK_Q.get(sport, {}).get(key), None
            at, src = BOOK_Q_AT.get(sport), "pull"
            if odds is None and key in ALT_Q.get(sport, {}):   # an alternate line: priced by the alternate pull
                odds, at = ALT_Q[sport][key], ALT_Q_AT.get((sport, x["event_id"]), at)
        if at is None:
            continue
        hhmm = at.astimezone(ET).strftime("%-I:%M %p")
        bk = BOOK_NAMES.get(x["book"], x["book"])
        if started:
            state, line_ = "started", f"⏱️ **Game started** -- {'last seen ' + price_str(odds, x['book'], pp) if odds is not None else 'off the board'} @ {bk}"
        elif odds is None:
            state, line_ = "gone", f"🔴 **Gone** as of {hhmm} -- no longer offered at {bk}"
        else:
            ev = _ev_now(sport, g, x["kind"], x["side"], x["line"], x["book"], odds)
            ok = evaluate(sport, g, x["kind"], x["side"], x["line"], x["book"], odds, now, pp)
            ps = price_str(odds, x["book"], pp)
            when = f"as of {hhmm}" if src == "live" else f"at the last {bk} check ({hhmm})"
            if ok:
                state, line_ = "good", f"🟢 **Still good** {when} · {ps} @ {bk} (EV {ok['ev']:+.1%})"
            elif ev is not None and ev > 0:
                state, line_ = "gone", f"🔴 **Gone** {when} -- edge down to {ev:+.1%} at {ps} @ {bk}"
            else:
                state, line_ = "gone", f"🔴 **Gone** {when} -- now {ps} @ {bk}" + (f" (EV {ev:+.1%})" if ev is not None else "")
        # Only what changes a decision is shown: 'Gone' when the price dies (removed if it comes back) and
        # 'Game started'. A still-good alert is left alone -- no edits every few minutes.
        prev_state = x.get("status") or "good"
        if state == prev_state:
            continue
        body = x.get("msg_text") or alert_text(x, x)
        if state == "good":
            line_ = ""
        x.update(status=state, status_line=line_, status_at=now.isoformat())
        if state == "gone" and not x.get("gone_at"):
            x["gone_at"] = now.isoformat()
        if state == "started":
            x["status_final"] = True
        discord_send(body + status_suffix(x), edit_id=x["msg_id"])
        edits += 1
        fs_set(f"scan_plays/{x.get('_id') or play_id(x, x['event_id'])}", {k: v for k, v in x.items() if k != "_id"})
    return edits


# ---------------- news: ESPN injury reports (free) ----------------
INJURY_FEED = {"nba": "basketball/nba", "nfl": "football/nfl", "nhl": "hockey/nhl"}
NEWS_PULL_GAP_MIN = 10     # news can pull sooner than the usual 15-minute gap


def injury_snapshot(sport):
    """{team display name: {(player id, status), ...}} or None if the feed is down / not covered. Status changes only:
    ESPN re-stamps entries whenever a note is edited, which would be noise."""
    path = INJURY_FEED.get(sport)
    if not path:
        return None
    try:
        d = _get(f"https://site.api.espn.com/apis/site/v2/sports/{path}/injuries") or {}
    except Exception:  # noqa: BLE001
        return None
    return {t.get("displayName", ""): {(i.get("id"), i.get("status")) for i in t.get("injuries", [])}
            for t in d.get("injuries", [])}


def injury_news(prev, cur, upcoming):
    """Teams playing in the next 12 hours whose injury report changed since the last check."""
    if prev is None or cur is None:
        return []
    teams = []
    for team, entries in cur.items():
        if entries != prev.get(team, set()) and any(name_hits(team, g["home"]) or name_hits(team, g["away"])
                                                    or name_hits(g["home"], team) or name_hits(g["away"], team)
                                                    for g in upcoming.values()):
            teams.append(team)
    return teams


# ---------------- watch loop ----------------
def load_fair(sport):
    d = fs_get(f"scan_state/fair_{sport}") or {}
    return d.get("games") or {}, d.get("pulled")


def save_fair(sport, games, pulled, mids):
    fs_set(f"scan_state/fair_{sport}", dict(games=games, pulled=pulled, mids=mids))


def close_lines(sport, games, plays, now):
    """Closing line for every logged play in a game about to start: consensus and Pinnacle at the play's line."""
    n = 0
    for p in plays:
        if p.get("sport") != sport or p.get("p_close") is not None:
            continue
        g = games.get(p["event_id"])
        if not g or (datetime.fromisoformat(g["start"]) - now).total_seconds() > CLOSE_WINDOW_MIN * 60:
            continue
        views = (g.get("views") or {}).get(p["kind"])
        if not views:
            continue
        pc = fair_side(sport, p["kind"], views, p["line"], p["side"])
        pp = fair_side(sport, p["kind"], views, p["line"], p["side"], only=SHARP)
        if pc is None:
            continue
        doc = {k: v for k, v in p.items() if k != "_id"}
        doc.update(p_close=round(pc, 4), p_close_pin=round(pp, 4) if pp is not None else None, close_at=now.isoformat())
        fs_set(f"scan_plays/{p.get('_id') or play_id(p, p['event_id'])}", doc)
        p.update(doc)
        n += 1
    return n


def bot_check():
    """One line in the log saying whether ✅/❌ tracking works (and why not)."""
    if not BOT:
        return log("bot: no DISCORD_BOT_TOKEN -- alerts post without ✅/❌ buttons")
    if not channel_id():
        return log("bot: couldn't find the alert channel from the webhook")
    try:
        bot("GET", f"/channels/{channel_id()}/messages", params=dict(limit=1))
        log("bot: connected -- can read the alert channel; ✅/❌ tracking on")
    except requests.HTTPError as e:
        code = e.response.status_code
        why = {401: "the token is wrong (reset it and update the secret)",
               403: "it can't see the channel (add the bot to #value-plays with View Channel + Read Message History)",
               404: "the channel wasn't found"}.get(code, f"HTTP {code}")
        log(f"bot: not working -- {why}")
    except Exception as e:  # noqa: BLE001
        log(f"bot: check failed ({type(e).__name__})")


def refresh_msg_doc(seen, now):
    """Swapped in whole, so the gateway thread never sees a half-built map."""
    global MSG_DOC
    MSG_DOC = {x["msg_id"]: x for x in list(seen.values())
               if x.get("alerted") and not x.get("superseded") and x.get("msg_id")
               and datetime.fromisoformat(x["start"]) > now - timedelta(hours=6)}


def watch(minutes, sports):
    global POST
    bot_check()
    bad = selftest()
    if bad:   # never post alerts off broken math
        log(f"SELF-TEST FAILED: {bad} -- alerts paused for this run")
        discord_send(f"🚨 **Scanner paused:** a math self-test failed ({', '.join(bad)}). No alerts until it's fixed.",
                     buttons=False)
        POST = False
    t_end = time.time() + minutes * 60
    seen = {p["_id"]: p for p in fs_recent("scan_plays", "start", (datetime.now(timezone.utc) - timedelta(days=2)).isoformat())}
    BOT_MSGS.update(x["msg_id"] for x in seen.values() if x.get("via") == "bot" and x.get("msg_id"))
    refresh_msg_doc(seen, datetime.now(timezone.utc))
    if bot_on():
        threading.Thread(target=gateway, daemon=True).start()
        for mid, x in list(MSG_DOC.items()):   # give alerts you haven't answered the current buttons
            if mid in BOT_MSGS and x.get("taken") is None:
                try:
                    bot("PATCH", f"/channels/{channel_id()}/messages/{mid}", json={"components": BUTTONS})
                    time.sleep(0.3)
                except Exception:  # noqa: BLE001
                    pass
    state = {}
    for s in sports:
        games, pulled = load_fair(s)
        state[s] = dict(games=games, pulled=pulled, mids={}, inj=injury_snapshot(s))
    cycle = 0
    while time.time() < t_end:
        cycle += 1
        now = datetime.now(timezone.utc)
        st = credits_state(now)
        n_alerts = n_quotes = n_edits = 0
        for s in sports:
            S = state[s]
            S["games"] = {k: g for k, g in S["games"].items() if datetime.fromisoformat(g["start"]) > now - timedelta(hours=8)}
            upcoming = {k: g for k, g in S["games"].items() if now < datetime.fromisoformat(g["start"]) < now + timedelta(hours=12)}
            pulled = datetime.fromisoformat(S["pulled"]) if S["pulled"] else None
            age_min = (now - pulled).total_seconds() / 60 if pulled else 1e9
            reasons = []
            if (age_min >= BASE_GAP_MIN and upcoming) or age_min >= 12 * 60:
                reasons.append("baseline")
            open_plays = [p for p in seen.values() if p.get("sport") == s and (p.get("alerted") or p.get("shadow"))
                          and not p.get("superseded")
                          and p.get("p_close") is None
                          and now < datetime.fromisoformat(p["start"]) < now + timedelta(minutes=CLOSE_WINDOW_MIN)]
            if open_plays and age_min >= 8:
                reasons.append("closing line")
            full = cycle % FULL_EVERY == 1
            q = exchange_quotes(s, S["games"], ml_only=not full) if upcoming else []
            n_quotes += len(q)
            exchange_mids(S["games"], q, now)
            note_disagreements(s, q)
            # line moves since the last pull
            moved = []
            for gid in upcoming:
                mid = ml_mid(q, gid)
                if mid is None:
                    continue
                base = S["mids"].setdefault(gid, mid)
                if abs(mid - base) >= MOVE_TRIGGER:
                    moved.append(gid)
            if moved:
                reasons.append(f"exchanges moved on {len(moved)} game(s)")
            cur = injury_snapshot(s)
            news = injury_news(S["inj"], cur, upcoming)
            if cur is not None and not news:
                S["inj"] = cur          # news stays pending until a pull has priced it
            if news:
                reasons.append(f"injury news ({len(news)} team(s))")
            # exchange prices that clear the bar
            found, stale = [], []
            for x in q:
                if x["size"] and x["size"] < MIN_SIZE_USD:
                    continue
                p = evaluate(s, x["g"], x["kind"], x["side"], x["line"], x["book"], x["odds"], now, x["prob"], allow_test=True)
                if not p:
                    continue
                if age_min < STALE_FAIR_MIN:
                    found.append((x["gid"], x["g"], p, x["size"]))
                else:   # confirm with a pull only if this price is new (or better than what's logged)
                    old = seen.get(play_id(p, x["gid"]))
                    if not old or payout(p["odds"], "x", s) > payout(old.get("odds_alerted") or old["odds"], "x", s) + 1e-9:
                        stale.append((x["gid"], x["g"], p, x["size"]))
            if stale:
                reasons.append(f"{len(stale)} exchange price(s) to confirm")
            if reasons and (age_min >= MIN_PULL_GAP_MIN or "closing line" in reasons
                            or (news and age_min >= NEWS_PULL_GAP_MIN)):
                games, plays = pull(s, now, st, ", ".join(reasons))
                if games is not None:
                    S["games"], S["pulled"], S["mids"] = games, now.isoformat(), {}
                    merge_alt_views(S)
                    if cur is not None:
                        S["inj"] = cur
                    for gid in games:   # re-base the move detector on this pull
                        mid = ml_mid(q, gid)
                        if mid is not None:
                            S["mids"][gid] = mid
                    found = plays
                    # re-check the exchange prices against the fresh line
                    q = exchange_quotes(s, S["games"])
                    for x in q:
                        if x["size"] and x["size"] < MIN_SIZE_USD:
                            continue
                        p = evaluate(s, x["g"], x["kind"], x["side"], x["line"], x["book"], x["odds"], now, x["prob"], allow_test=True)
                        if p:
                            found.append((x["gid"], x["g"], p, x["size"]))
                    n_quotes += len(q)
                    close_lines(s, S["games"], list(seen.values()), now)
                    save_fair(s, S["games"], S["pulled"], S["mids"])
            if s in ALT_SPORTS and upcoming:
                try:
                    found += pull_alts(s, S, now, st)
                except Exception as e:  # noqa: BLE001 -- alternates never stop the scan
                    log(f"[{s}] alternate lines: {type(e).__name__}: {str(e)[:80]}")
            if found:
                apply_taps(seen)   # your latest taps (Bet it / Line moved / Pass) count before anything new is posted
                n_alerts += record(found, seen, now)
            try:
                n_edits += live_status(s, S["games"], q, seen, now, full or bool(reasons and S["pulled"] == now.isoformat()))
            except Exception as e:  # noqa: BLE001 -- status lines never stop the scan
                log(f"[{s}] live status: {type(e).__name__}: {str(e)[:80]}")
        try:   # ✅ / ❌ reactions on webhook-posted alerts (one Discord call a minute; needs DISCORD_BOT_TOKEN)
            read_reactions(seen, now)
        except Exception as e:  # noqa: BLE001
            log(f"reactions: {type(e).__name__}")
        refresh_msg_doc(seen, now)
        apply_taps(seen)
        log(f"cycle {cycle}: {n_quotes} exchange prices checked, {n_alerts} alert(s), {n_edits} status update(s); "
            f"credits {st['used']}/{CREDIT_CAP}")
        time.sleep(max(0, min(CYCLE_S, t_end - time.time())))
    try:   # free (ESPN finals): tonight's finished games are graded before the run ends
        grade()
    except Exception as e:  # noqa: BLE001
        log(f"end-of-run grade failed: {type(e).__name__}")
    end = datetime.now(timezone.utc)
    try:
        fs_set("scan_state/health", {"day": end.astimezone(ET).strftime("%Y-%m-%d"), **HEALTH})
    except Exception:  # noqa: BLE001
        pass
    if end.astimezone(ET).hour >= 23 or end.astimezone(ET).hour < 5:   # the night's last run: post the wrap-up
        try:
            txt = day_summary(end)
            if txt:
                discord_send(txt, buttons=False)
        except Exception as e:  # noqa: BLE001
            log(f"summary failed: {type(e).__name__}")


def _band_lines(xs, bands):
    out = []
    for lo, hi, label in bands:
        ys = [p for p in xs if lo <= (p.get("ev") or 0) < hi]
        if not ys:
            continue
        w = sum(p["result"] == "win" for p in ys); l_ = sum(p["result"] == "loss" for p in ys)
        cl = [p["clv"] for p in ys if p.get("clv") is not None]
        out.append(f"`{label:<15s}` {len(ys):>3d} · {w}-{l_}"
                   + (f" · beat close {100 * sum(c > 0 for c in cl) / len(cl):.0f}% · CLV {sum(cl) / len(cl):+.1%}" if cl else ""))
    return out


def morning_report(now):
    """Posted to Discord after the morning grade: your card (yesterday's logged bets with CLV, day + running totals)
    and the scorecard for every edge size, posted and logged."""
    yday = (now.astimezone(ET) - timedelta(days=1)).strftime("%Y-%m-%d")
    done = fs_get("scan_state/morning") or {}
    if done.get("day") == yday:   # several grade runs a morning (GitHub's schedule is unreliable): post once
        log(f"morning report for {yday} already posted")
        return
    allp = [p for p in fs_recent("scan_plays", "start", (now - timedelta(days=60)).isoformat())
            if not p.get("superseded") and p.get("result") in ("win", "loss", "push")]
    day_of = lambda p: datetime.fromisoformat(p["start"]).astimezone(ET).strftime("%Y-%m-%d")
    mine_all = [p for p in allp if p.get("taken")]
    mine_day = sorted((p for p in mine_all if day_of(p) == yday), key=lambda p: p["start"])
    icon = {"win": "✅", "loss": "❌", "push": "➖"}

    def mine_totals(ps):
        res = lambda p: p.get("result_taken") or p["result"]
        w = sum(res(p) == "win" for p in ps); l_ = sum(res(p) == "loss" for p in ps)
        usd = sum(p.get("pl_taken_usd") or 0 for p in ps)
        ct = [p["clv_taken"] for p in ps if p.get("clv_taken") is not None]
        return (f"{w}-{l_}, {usd:+,.0f}" + (f" · beat the sharp close {sum(c > 0 for c in ct)}/{len(ct)} (avg {sum(ct) / len(ct):+.1%})"
                                             if ct else ""))
    lines = [f"📊 **Morning report · {datetime.fromisoformat(yday).strftime('%a %b %-d')}**", "", "**Your card**"]
    if mine_day:
        for p in mine_day[:25]:
            res = p.get("result_taken") or p["result"]
            stake = p.get("stake_taken") or p.get("stake_usd") or 0
            ps_ = price_str(p.get("taken_odds") or p["odds"], p["book"], p.get("taken_price"))
            c = p.get("clv_taken")
            lines.append(f"{icon[res]} {SPORTS[p['sport']]['label']} {p['label']} · ${stake:g} @ {ps_} "
                         f"{BOOK_NAMES.get(p['book'], p['book'])} · {p.get('pl_taken_usd') or 0:+.2f}"
                         + (f" · CLV {c:+.1%}" if c is not None else ""))
        if len(mine_day) > 25:
            lines.append(f"…and {len(mine_day) - 25} more")
        lines.append(f"**Day:** {mine_totals(mine_day)}")
    else:
        lines.append("No logged bets graded for that day (tap Bet it on the alerts you take).")
    if mine_all:
        lines.append(f"**Since you started logging:** {mine_totals(mine_all)} over {len(mine_all)} bets")
    recent = [p for p in allp if p.get("found") and datetime.fromisoformat(p["start"]) > now - timedelta(days=30)]
    posted = [p for p in recent if p.get("alerted")]
    logged = [p for p in recent if p.get("test")]
    try:
        lines += [""] + health_report(now)
    except Exception as e:  # noqa: BLE001
        lines += ["", f"🩺 checks could not run: {type(e).__name__}"]
    lines += ["", "**Scorecard · last 30 days** (posted = alerts · logged = tests, never posted)"]
    lines += _band_lines(logged, [(0.01, 0.02, "1-2% logged")])
    lines += _band_lines(posted, [(0.02, 0.04, "2-4% posted"), (0.04, 0.07, "4-7% posted"), (0.07, 0.121, "7-12% posted")])
    lines += _band_lines(logged, [(0.121, 0.31, "12-30% logged")])
    if posted:
        w = sum(p["result"] == "win" for p in posted); l_ = sum(p["result"] == "loss" for p in posted)
        cl = [p["clv"] for p in posted if p.get("clv") is not None]
        lines.append(f"**All alerts:** {len(posted)} · {w}-{l_} · {sum(p.get('pl_usd') or 0 for p in posted):+,.0f} at suggested stakes"
                     + (f" · beat close {100 * sum(c > 0 for c in cl) / len(cl):.0f}% · CLV {sum(cl) / len(cl):+.1%}" if cl else ""))
    fs_set("scan_state/morning", {"day": yday, "at": now.isoformat()})
    # Discord caps a message at 2000 characters: split on lines
    chunk = ""
    for ln in lines + [None]:
        if ln is None or len(chunk) + len(ln) + 1 > 1900:
            if chunk.strip():
                discord_send(chunk, buttons=False)
            chunk = ""
        if ln is not None:
            chunk += ln + "\n"


def day_summary(now):
    """Tonight's wrap-up for Discord: alerts, what you took, money still out, results so far, how long alerts lasted."""
    day = (now - timedelta(hours=5)).astimezone(ET).strftime("%Y-%m-%d")   # after midnight still counts as 'today'
    ps = [x for x in fs_recent("scan_plays", "start", (now - timedelta(days=2)).isoformat())
          if x.get("alerted") and not x.get("superseded")
          and datetime.fromisoformat(x.get("alerted_at") or x["start"]).astimezone(ET).strftime("%Y-%m-%d") == day]
    if not ps:
        return None
    by_sport = {}
    for x in ps:
        by_sport[SPORTS[x["sport"]]["label"]] = by_sport.get(SPORTS[x["sport"]]["label"], 0) + 1
    lines = [f"📋 **Wrap-up {datetime.fromisoformat(day).strftime('%a %b %-d')}**",
             f"Alerts: {len(ps)} (" + ", ".join(f"{k} {v}" for k, v in sorted(by_sport.items())) + ")"]
    if any(x.get("taken") is not None for x in ps):
        tk = [x for x in ps if x.get("taken")]
        mv = sum(x.get("taken") is False and x.get("pass_reason") == "line moved" for x in ps)
        lines.append(f"You took {len(tk)} (${sum(x.get('stake_taken') or x.get('stake_usd') or 0 for x in tk):,.0f}), "
                     f"passed {sum(x.get('taken') is False for x in ps) - mv}, missed (line moved) {mv}, "
                     f"no answer {sum(x.get('taken') is None for x in ps)}")
    pend = [x for x in ps if not x.get("result") and x.get("taken") is not False]
    if pend:
        lines.append(f"Still open: {len(pend)} bets, ${sum(x.get('stake_taken') or x.get('stake_usd') or 0 for x in pend):,.0f}")
    gr = [x for x in ps if x.get("result") in ("win", "loss", "push")]
    if gr:
        w = sum(x["result"] == "win" for x in gr); l_ = sum(x["result"] == "loss" for x in gr)
        usd = sum(x.get("pl_usd") or 0 for x in gr)
        cl = [x["clv"] for x in gr if x.get("clv") is not None]
        lines.append(f"Graded so far: {w}-{l_}, {usd:+,.0f} at the suggested stakes"
                     + (f", beat the close {sum(c > 0 for c in cl)}/{len(cl)} (avg {sum(cl) / len(cl):+.1%})" if cl else ""))
        mine = [x for x in gr if x.get("taken")]
        if mine:
            ct = [x["clv_taken"] for x in mine if x.get("clv_taken") is not None]
            lines.append(f"Your bets: {sum(x['result'] == 'win' for x in mine)}-{sum(x['result'] == 'loss' for x in mine)}, "
                         f"{sum(x.get('pl_taken_usd') or 0 for x in mine):+,.0f} at your prices and stakes"
                         + (f", beat the sharp close {sum(c > 0 for c in ct)}/{len(ct)} (avg {sum(ct) / len(ct):+.1%})" if ct else ""))
    life = {}
    for x in ps:
        if x.get("gone_at") and x.get("alerted_at"):
            mins = (datetime.fromisoformat(x["gone_at"]) - datetime.fromisoformat(x["alerted_at"])).total_seconds() / 60
            life.setdefault(BOOK_NAMES.get(x["book"], x["book"]), []).append(mins)
    if life:
        lines.append("How long alerts lasted (median): " + ", ".join(
            f"{b} {sorted(v)[len(v) // 2]:.0f} min" for b, v in sorted(life.items(), key=lambda kv: -len(kv[1]))))
    return "\n".join(lines)


# ---------------- grading ----------------
ESPN = {"nfl": "football/nfl", "nba": "basketball/nba", "ncaab": "basketball/mens-college-basketball", "nhl": "hockey/nhl",
        "ncaaf": "football/college-football"}


def espn_finals(sport, day):
    if SPORTS[sport].get("odds_prefix"):
        return []   # tennis: graded from Odds API scores instead
    params = dict(dates=day.replace("-", ""), limit=500)
    if sport == "ncaab":
        params["groups"] = 50
    if sport == "ncaaf":
        params["groups"] = 80   # FBS
    d = _get(f"https://site.api.espn.com/apis/site/v2/sports/{SPORTS[sport].get('espn') or ESPN[sport]}/scoreboard", params) or {}
    out = []
    for e in d.get("events", []):
        c = (e.get("competitions") or [{}])[0]
        if not c.get("status", {}).get("type", {}).get("completed"):
            continue
        teams = {t["homeAway"]: t for t in c.get("competitors", [])}
        if set(teams) != {"home", "away"}:
            continue
        nm = lambda t: [t["team"].get(k) for k in ("displayName", "shortDisplayName", "location", "name") if t["team"].get(k)]
        out.append(dict(home=nm(teams["home"]), away=nm(teams["away"]),
                        hs=float(teams["home"].get("score") or 0), as_=float(teams["away"].get("score") or 0)))
    return out


def tennis_finals(okey):
    """Finished tennis matches for one tournament from the Odds API (scores endpoint, 2 credits)."""
    key = os.environ.get("ODDS_API_KEY")
    now = datetime.now(timezone.utc)
    st = credits_state(now)
    if not key or not can_spend(st, now, 2):
        return []
    r = requests.get(f"{ODDS}/{okey}/scores", params=dict(apiKey=key, daysFrom=3), timeout=30)
    if r.status_code in (404, 422):
        return []
    r.raise_for_status()
    charge(st, now, int(r.headers.get("x-requests-last") or 2))
    out = []
    for e in r.json():
        if not e.get("completed") or not e.get("scores"):
            continue
        sc = {x["name"]: float(x.get("score") or 0) for x in e["scores"]}
        out.append(dict(home=[e["home_team"]], away=[e["away_team"]], hs=sc.get(e["home_team"], 0), as_=sc.get(e["away_team"], 0)))
    return out


def grade_result(p, hs, as_):
    """win / loss / push for a play given the final home and away scores."""
    if p["kind"] == "h2h" and (p["side"] == "d" or SPORTS[p["sport"]].get("three_way")):
        m = hs - as_     # soccer: 90 minutes + stoppage; a draw loses both team bets
        return "win" if (m == 0) == (p["side"] == "d") and (p["side"] == "d" or (m > 0) == (p["side"] == "a")) else "loss"
    if p["kind"] == "h2h":
        m = hs - as_
    elif p["kind"] == "spread":
        m = hs - as_ + p["line"]
    else:
        m = hs + as_ - p["line"]
    return "push" if m == 0 else ("win" if (m > 0) == (p["side"] == "a") else "loss")


# ---------------- health checks ----------------
def selftest():
    """Known answers the math must get right. Any failure stops alerts (returns the list of failures)."""
    bad = []

    def check(name, ok):
        if not ok:
            bad.append(name)
    check("devig -110/-110 = 50%", abs(devig(prob(-110), prob(-110)) - 0.5) < 1e-6)
    check("devig sums to 1", abs(devig(prob(-150), prob(130)) + devig(prob(130), prob(-150)) - 1) < 1e-6)
    check("power devig shades the longshot", devig(prob(950), prob(-1450)) < prob(950) / (prob(950) + prob(-1450)))
    check("3-way devig sums to 1", abs(sum(devig_n([prob(130), prob(215), prob(240)])) - 1) < 1e-6)
    check("payout +100 = 1.0", abs(payout(100, "fanduel", "nba") - 1.0) < 1e-9)
    check("payout -110 = 0.909", abs(payout(-110, "fanduel", "nba") - 100 / 110) < 1e-9)
    check("Kalshi fee lowers payout", payout(100, "kalshi", "nba") < 1.0 and payout(100, "kalshi", "nba") > 0.93)
    check("Novig has no pregame fee", abs(payout(100, "novig", "nba") - 1.0) < 1e-9)
    check("american(0.5) = -100/+100", abs(american(0.5)) == 100)
    check("price_str exchange cents", price_str(115, "novig", 0.465).startswith("46.5"))
    g = dict(views={"spread": [[b, w, -4.0, 0.5] for b, w in (("pinnacle", 3), ("fanduel", 1), ("betmgm", 1), ("betrivers", 1))]})
    pa = fair_side("ncaab", "spread", g["views"]["spread"], -4.0, "a")
    pb = fair_side("ncaab", "spread", g["views"]["spread"], -4.0, "b")
    check("fair at the market line = 50%", pa is not None and abs(pa - 0.5) < 1e-6 and abs(pa + pb - 1) < 1e-9)
    p5 = fair_side("ncaab", "spread", g["views"]["spread"], -5.0, "a")
    check("laying an extra point lowers the favorite's chance", p5 is not None and 0.42 < p5 < 0.5)
    t = lambda kind, side, line, hs, as_, sp="nba": grade_result(dict(sport=sp, kind=kind, side=side, line=line), hs, as_)
    check("home -3.5 wins by 4 = win", t("spread", "a", -3.5, 104, 100) == "win")
    check("away +3.5 loses by 4 = loss", t("spread", "b", -3.5, 104, 100) == "loss")
    check("home -4 wins by 4 = push", t("spread", "a", -4.0, 104, 100) == "push")
    check("over 200.5 at 201 = win", t("total", "a", 200.5, 101, 100) == "win")
    check("under 200.5 at 201 = loss", t("total", "b", 200.5, 101, 100) == "loss")
    check("away ML wins", t("h2h", "b", None, 99, 100) == "win")
    check("soccer draw: home ML loses", t("h2h", "a", None, 1, 1, "epl") == "loss")
    check("soccer draw: draw wins", t("h2h", "d", None, 1, 1, "epl") == "win")
    check("soccer: draw loses when home wins", t("h2h", "d", None, 2, 1, "epl") == "loss")
    return bad


def health_report(now):
    """Daily checks on real plays (last 2 days). Returns lines for the morning report; '' items mean all clear."""
    ps = [p for p in fs_recent("scan_plays", "start", (now - timedelta(days=2)).isoformat()) if not p.get("superseded")]
    out, warn = [], 0
    bad = selftest()
    out.append("🩺 **Checks**")
    out.append("✅ math self-test passed" if not bad else f"🚨 math self-test FAILED: {', '.join(bad)} -- alerts are paused")
    warn += bool(bad)
    al = [p for p in ps if p.get("alerted")]
    far = [p for p in al if (p.get("fv") or {}).get("pin") is not None and abs(p["p_fair"] - p["fv"]["pin"]) > 0.06]
    if far:
        warn += 1
        out.append(f"⚠️ {len(far)} alert(s) where our fair price was 6+ points from Pinnacle's -- e.g. "
                   + "; ".join(f"{SPORTS[p['sport']]['label']} {p['label']} ({p['p_fair']:.0%} vs {p['fv']['pin']:.0%})" for p in far[:3]))
    worse = [p for p in al if p.get("clv") is not None and p["clv"] < -0.08]
    if worse:
        warn += 1
        out.append(f"⚠️ {len(worse)} alert(s) closed 8%+ worse than our price (stale or wrong number?) -- e.g. "
                   + "; ".join(f"{SPORTS[p['sport']]['label']} {p['label']} @ {BOOK_NAMES.get(p['book'], p['book'])} ({p['clv']:+.0%})" for p in worse[:3]))
    mism = [p for p in ps if p.get("final_hs") is not None and p.get("result") in ("win", "loss", "push")
            and grade_result(p, p["final_hs"], p["final_as"]) != p["result"]]
    if mism:
        warn += 1
        out.append(f"🚨 {len(mism)} graded result(s) don't match the saved final score -- e.g. "
                   + "; ".join(f"{p['label']} ({p['result']})" for p in mism[:3]))
    nores = [p for p in ps if p.get("result") == "no result"]
    if nores:
        out.append(f"ℹ️ {len(nores)} play(s) couldn't be matched to a final score (not graded)")
    exd = (fs_get("scan_state/health") or {}).get("exchange_disagree") or {}
    if sum(exd.values()) > 0:
        out.append("ℹ️ exchange prices disagreeing by 15+ points (same side): " + ", ".join(f"{k} {v}" for k, v in exd.items() if v))
    recent = [p for p in fs_recent("scan_plays", "start", (now - timedelta(days=21)).isoformat())
              if p.get("alerted") and not p.get("superseded") and p.get("clv") is not None]
    for sport in SPORTS:
        xs = sorted((p for p in recent if p["sport"] == sport), key=lambda p: p["start"])[-50:]
        if len(xs) >= 20 and sum(p["clv"] for p in xs) / len(xs) < -0.005:
            warn += 1
            out.append(f"⚠️ {SPORTS[sport]['label']}: last {len(xs)} alerts are LOSING to the close on average "
                       f"({sum(p['clv'] for p in xs) / len(xs):+.1%}) -- worth a look before betting more of them")
    if warn == 0:
        out.append("✅ no pricing, matching or grading problems found")
    return out


def grade():
    now = datetime.now(timezone.utc)
    plays = [p for p in fs_recent("scan_plays", "start", (now - timedelta(days=6)).isoformat()) if not p.get("result")
             and datetime.fromisoformat(p["start"]) < now - timedelta(hours=4)]
    cache, done = {}, 0
    for p in plays:
        day = datetime.fromisoformat(p["start"]).astimezone(ET).strftime("%Y-%m-%d")
        key = ("okey", p.get("okey")) if SPORTS[p["sport"]].get("odds_prefix") else (p["sport"], day)
        if key not in cache:
            cache[key] = tennis_finals(p["okey"]) if key[0] == "okey" and p.get("okey") else espn_finals(*key) if key[0] != "okey" else []
        fin = [f for f in cache[key] if any(name_hits(n, p["home"]) or name_hits(p["home"], n) for n in f["home"])
               and any(name_hits(n, p["away"]) or name_hits(p["away"], n) for n in f["away"])]
        if len(fin) != 1:
            if datetime.fromisoformat(p["start"]) < now - timedelta(days=4):
                fs_set(f"scan_plays/{p['_id']}", {**{k: v for k, v in p.items() if k != "_id"}, "result": "no result"})
            continue
        f = fin[0]
        if (p.get("alerted_at") or p.get("found") or "") >= p["start"]:
            fs_set(f"scan_plays/{p['_id']}", {**{k: v for k, v in p.items() if k != "_id"},
                                             "result": "void: posted after the start", "graded_at": now.isoformat()})
            continue
        res = grade_result(p, f["hs"], f["as_"])
        pay = payout(p["odds"], p["book"], p["sport"])
        pl = {"win": pay, "loss": -1.0, "push": 0.0}[res]
        doc = {k: v for k, v in p.items() if k != "_id"}
        if p.get("taken"):   # your bet at the price and stake you actually got
            pay_t = payout(p.get("taken_odds") or p["odds"], p["book"], p["sport"])
            doc["pl_taken_usd"] = round({"win": pay_t, "loss": -1.0, "push": 0.0}[res]
                                        * (p.get("stake_taken") or p.get("stake_usd") or 0), 2)
            if p.get("p_close") is not None:   # strict CLV at YOUR price: vs the sharp no-vig consensus at close
                doc["clv_taken"] = round(p["p_close"] * (1 + pay_t) - 1, 4)
        doc.update(result=res, pl_unit=round(pl, 4), pl_usd=round(pl * (p.get("stake_usd") or 0), 2),
                   clv=round(p["p_close"] * (1 + pay) - 1, 4) if p.get("p_close") is not None else None,
                   clv_pin=round(p["p_close_pin"] * (1 + pay) - 1, 4) if p.get("p_close_pin") is not None else None,
                   graded_at=now.isoformat(), final_hs=f["hs"], final_as=f["as_"])
        fs_set(f"scan_plays/{p['_id']}", doc)
        done += 1
    allp = [p for p in fs_list("scan_plays") if p.get("result") in ("win", "loss", "push") and p.get("alerted")
            and not p.get("superseded")]
    if allp:
        cl = [p["clv"] for p in allp if p.get("clv") is not None]
        w = sum(p["result"] == "win" for p in allp); l = sum(p["result"] == "loss" for p in allp)
        log(f"graded {done}; alerted plays to date: {w}-{l}, {sum(p.get('pl_usd') or 0 for p in allp):+.0f} USD, "
            f"avg CLV {sum(cl) / len(cl):+.2%} over {len(cl)}, beat close {sum(c > 0 for c in cl) / max(len(cl), 1):.0%}"
            if cl else f"graded {done}; {w}-{l}")
    else:
        log(f"graded {done}")
    rep_ = book_report(now)
    if rep_:
        print("book scorecard this month:\n" + rep_)
    print(timing_report(now))


def fair_report(ps):
    """Which fair-line method best predicts Pinnacle's no-vig close, and whose 2%+ picks beat it most."""
    xs = [p for p in ps if p.get("fv") and p.get("p_close_pin") is not None]
    if len(xs) < 10:
        return [f"  fair-line test: {len(xs)} plays with variants + Pinnacle close so far"]
    out = ["  fair-line test (truth = Pinnacle no-vig close): method / plays / avg miss / 2%+ picks / beat close / CLV"]
    for v in ("live", "pin", "equal", "fresh", "exblend"):
        ys = [p for p in xs if v in p["fv"]]
        if not ys:
            continue
        miss = sum(abs(p["fv"][v] - p["p_close_pin"]) for p in ys) / len(ys)
        picks = [p for p in ys if p["fv"][v] * (1 + payout(p["odds"], p["book"], p["sport"])) - 1 >= 0.02]
        clv = [p["p_close_pin"] * (1 + payout(p["odds"], p["book"], p["sport"])) - 1 for p in picks]
        out.append(f"  {v:8s} {len(ys):5d}  {miss * 100:5.2f} pts  {len(picks):4d}"
                   + (f"  {sum(c > 0 for c in clv) / len(clv):5.0%}  {sum(clv) / len(clv):+6.2%}" if clv else ""))
    return out


def timing_report(now, days=30):
    """CLV by how far before the start each play was found -- posted and log-only plays (totals only)."""
    ps = [p for p in fs_recent("scan_plays", "start", (now - timedelta(days=days)).isoformat())
          if p.get("clv") is not None and not p.get("superseded") and p.get("found")]
    if not ps:
        return "timing: no graded plays with a closing line yet"
    out = ["timing (last 30 days): hours before start found / plays / avg CLV / beat close / record"]
    for lo, hi in ((0, 1), (1, 3), (3, 6), (6, 12), (12, 24), (24, 999)):
        xs = [p for p in ps if lo <= (datetime.fromisoformat(p["start"]) - datetime.fromisoformat(p["found"])).total_seconds() / 3600 < hi]
        if xs:
            w = sum(p["result"] == "win" for p in xs); l_ = sum(p["result"] == "loss" for p in xs)
            out.append(f"  {lo:>2}-{hi:<3}h {len(xs):5d}  {sum(p['clv'] for p in xs) / len(xs):+6.2%}  "
                       f"{sum(p['clv'] > 0 for p in xs) / len(xs):5.0%}  {w}-{l_}")
    hrs = lambda p: (datetime.fromisoformat(p["start"]) - datetime.fromisoformat(p["found"])).total_seconds() / 3600
    for sport in SPORTS:   # by sport: under / over 6 and 12 hours (NHL moves late on goalies, NFL early in the week...)
        xs = [p for p in ps if p.get("sport") == sport]
        if len(xs) < 5:
            continue
        cells = []
        for lo, hi in ((0, 6), (6, 12), (12, 999)):
            ys = [p for p in xs if lo <= hrs(p) < hi]
            cells.append(f"{lo}-{hi if hi < 999 else '+'}h: {len(ys)} @ {sum(p['clv'] for p in ys) / len(ys):+.1%}" if ys else f"{lo}-{hi if hi < 999 else '+'}h: -")
        out.append(f"  {SPORTS[sport]['label']:10s} " + " | ".join(cells))
    out += fair_report([p for p in fs_recent("scan_plays", "start", (now - timedelta(days=days)).isoformat())
                        if p.get("result") in ("win", "loss", "push") and not p.get("superseded")])
    # every edge size: posted alerts and logged tests (1-2% under the bar, 12-30% over the cap)
    out.append("  edge size: plays / avg CLV / beat close / record   (posted = alerts; logged = tests, never posted)")
    for lo, hi in ((0.01, 0.02), (0.02, 0.04), (0.04, 0.07), (0.07, 0.12), (0.12, 0.30)):
        for label, sub in (("posted", [p for p in ps if p.get("alerted")]), ("logged", [p for p in ps if p.get("test")])):
            xs = [p for p in sub if lo <= (p.get("ev") or 0) < hi]
            if xs:
                w = sum(p["result"] == "win" for p in xs); l_ = sum(p["result"] == "loss" for p in xs)
                out.append(f"  EV {lo:>4.0%}-{hi:<4.0%} {label:6s} {len(xs):4d}  {sum(p['clv'] for p in xs) / len(xs):+6.2%}  "
                           f"{sum(p['clv'] > 0 for p in xs) / len(xs):5.0%}  {w}-{l_}")
    # the sharp-market test: 1%-to-bar plays (logged only), by sport and EV band
    for sport in TEST_SPORTS:
        for lo, hi in ((0.01, 0.02), (0.02, 0.03), (0.03, 1)):
            xs = [p for p in ps if p.get("sport") == sport and lo <= (p.get("ev") or 0) < hi
                  and (p.get("test") or (lo >= 0.03 and p.get("alerted")))]
            if xs:
                w = sum(p["result"] == "win" for p in xs); l_ = sum(p["result"] == "loss" for p in xs)
                out.append(f"  test {SPORTS[sport]['label']:4s} EV {lo:.0%}-{hi if hi < 1 else 99:.0%} {len(xs):4d}  "
                           f"{sum(p['clv'] for p in xs) / len(xs):+6.2%}  {sum(p['clv'] > 0 for p in xs) / len(xs):5.0%}  {w}-{l_}")
    # main line vs alternate lines (points from Pinnacle's line when found)
    for lo, hi, name in ((0, 0.01, "main line"), (0.01, 1.01, "alt 0.5-1 pt"), (1.01, 3.01, "alt 1.5-3 pts"), (3.01, 99, "alt 3+ pts")):
        xs = [p for p in ps if p.get("kind") != "h2h" and lo <= (p.get("off_main") or 0) < hi and "off_main" in p]
        if xs:
            w = sum(p["result"] == "win" for p in xs); l_ = sum(p["result"] == "loss" for p in xs)
            out.append(f"  {name:14s} {len(xs):4d}  {sum(p['clv'] for p in xs) / len(xs):+6.2%}  "
                       f"{sum(p['clv'] > 0 for p in xs) / len(xs):5.0%}  {w}-{l_}")
    # per-sport posting windows can be set with SCAN_POST_WITHIN_<SPORT> once this says so
    for label, sub in (("posted", [p for p in ps if p.get("alerted")]), ("logged only", [p for p in ps if p.get("shadow")])):
        if sub:
            out.append(f"  {label:12s} {len(sub):5d}  {sum(p['clv'] for p in sub) / len(sub):+6.2%}  "
                       f"{sum(p['clv'] > 0 for p in sub) / len(sub):5.0%}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["watch", "pull", "grade", "books", "cbbbooks", "taken", "cfbrecord", "timing", "day",
                                     "altprobe", "fields"])
    ap.add_argument("sport", nargs="?", default="")
    ap.add_argument("--minutes", type=float, default=330)
    ap.add_argument("--sports", default=os.environ.get("SCAN_SPORTS") or ",".join(SPORTS))
    a = ap.parse_args()
    sports = [s for s in (a.sport or a.sports).split(",") if s in SPORTS]
    if a.mode == "grade":
        grade()
        try:
            morning_report(datetime.now(timezone.utc))
        except Exception as e:  # noqa: BLE001
            log(f"morning report failed: {type(e).__name__}: {str(e)[:100]}")
        return
    if a.mode == "altprobe":   # which books return alternate spreads / totals (counts only), ~2 credits per sport
        key = os.environ["ODDS_API_KEY"]
        for sk in ("basketball_nba", "icehockey_nhl", "americanfootball_nfl", "basketball_ncaab", "soccer_epl"):
            evs = requests.get(f"{ODDS}/{sk}/events", params=dict(apiKey=key), timeout=30).json()
            evs = [e for e in evs if e["commence_time"] > datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")]
            if not evs:
                print(sk, "no upcoming events"); continue
            e = evs[0]
            r = requests.get(f"{ODDS}/{sk}/events/{e['id']}/odds", timeout=30, params=dict(
                apiKey=key, bookmakers=",".join(BOOKS + ["kalshi", "polymarket"]), oddsFormat="american",
                markets="alternate_spreads,alternate_totals"))
            print(sk, "status", r.status_code, "credits", r.headers.get("x-requests-last"))
            if r.status_code != 200:
                print("  ", r.text[:200]); continue
            for bm in r.json().get("bookmakers", []):
                for mk in bm.get("markets", []):
                    pts = sorted({o.get("point") for o in mk["outcomes"] if o.get("point") is not None})
                    print(f"   {bm['key']:14s} {mk['key']:18s} {len(mk['outcomes']):3d} prices, lines {pts[:3]}..{pts[-3:]}")
        return
    if a.mode == "day":   # grade now, then yesterday's alerts split into the bug-era plays and the rest (totals only)
        grade()
        now = datetime.now(timezone.utc)
        day = (now - timedelta(hours=5)).astimezone(ET).strftime("%Y-%m-%d")
        ps = [x for x in fs_recent("scan_plays", "start", (now - timedelta(days=2)).isoformat())
              if x.get("alerted") and not x.get("superseded")
              and datetime.fromisoformat(x.get("alerted_at") or x["start"]).astimezone(ET).strftime("%Y-%m-%d") == day]

        def bucket(x):
            if (x.get("alerted_at") or "") >= x["start"]:
                return "bug: posted after the start"
            if (x.get("p_fair") or 1) < LONGSHOT_P and (x.get("alerted_at") or "") < "2026-11-22T17:20":
                return "bug: longshot math (before fix)"
            return "real value plays"

        def line(label, xs):
            gr = [x for x in xs if x.get("result") in ("win", "loss", "push")]
            w = sum(x["result"] == "win" for x in gr); l_ = sum(x["result"] == "loss" for x in gr)
            usd = sum(x.get("pl_usd") or 0 for x in gr)
            mine = [x for x in gr if x.get("taken")]
            cl = [x["clv"] for x in gr if x.get("clv") is not None]
            print(f"{label:34s} alerts {len(xs):3d} | graded {len(gr):3d}: {w}-{l_}, {usd:+.0f} at suggested stakes"
                  + (f" | beat close {sum(c > 0 for c in cl)}/{len(cl)} avg {sum(cl) / len(cl):+.1%}" if cl else " | no closing lines")
                  + (f" | your bets {sum(x['result'] == 'win' for x in mine)}-{sum(x['result'] == 'loss' for x in mine)} "
                     f"{sum(x.get('pl_taken_usd') or 0 for x in mine):+.0f}"
                     + (lambda ct: f" CLV {sum(c > 0 for c in ct)}/{len(ct)} {sum(ct) / len(ct):+.1%}" if ct else "")(
                         [x["clv_taken"] for x in mine if x.get("clv_taken") is not None]) if mine else ""))
        print(f"alerts posted {day}: {len(ps)}")
        for b in ("real value plays", "bug: posted after the start", "bug: longshot math (before fix)"):
            xs = [x for x in ps if bucket(x) == b]
            if xs:
                line(b, xs)
        for sport in SPORTS:
            xs = [x for x in ps if x["sport"] == sport and bucket(x) == "real value plays"]
            if xs:
                line(f"  real plays: {SPORTS[sport]['label']}", xs)
        for bk in sorted({x["book"] for x in ps}):
            xs = [x for x in ps if x["book"] == bk and bucket(x) == "real value plays"]
            if xs:
                line(f"  real plays @ {BOOK_NAMES.get(bk, bk)}", xs)
        pend = [x for x in ps if not x.get("result")]
        print(f"not graded yet: {len(pend)} (games not final, or no ESPN match)")
        return
    if a.mode == "fields":   # health check: are the tracking fields being saved on today's plays? (counts only)
        now = datetime.now(timezone.utc)
        ps = [x for x in fs_recent("scan_plays", "start", (now - timedelta(hours=6)).isoformat())
              if (x.get("found") or x.get("alerted_at") or "") >= (now - timedelta(hours=10)).isoformat()]
        print(f"plays found in the last 10h: {len(ps)} (posted {sum(1 for x in ps if x.get('alerted'))})")
        for f in ("fv", "off_main", "test", "overcap", "early", "shadow", "rel", "msg_id", "taken", "tapped_at", "clv_taken",
                  "p_close", "result"):
            print(f"  {f:10s} {sum(1 for x in ps if x.get(f) not in (None, False, ''))}")
        print("  fv methods:", {k: sum(1 for x in ps if k in (x.get('fv') or {})) for k in ("live", "pin", "equal", "fresh", "exblend")})
        print("  by sport:", {SPORTS[s_]['label']: sum(1 for x in ps if x['sport'] == s_) for s_ in SPORTS if any(x['sport'] == s_ for x in ps)})
        print("  credits:", (fs_get("scan_state/credits") or {}).get("used"), "this month;",
              ((fs_get("scan_state/credits") or {}).get("days") or {}))
        print("  morning report posted for:", (fs_get("scan_state/morning") or {}).get("day"))
        return
    if a.mode == "timing":
        return print(timing_report(datetime.now(timezone.utc)))
    if a.mode == "cfbrecord":   # CFB plays posted to Discord this season, by kind (totals only -- public log)
        st = fs_get("cfb_odds_state/current") or {}
        ps = [x for x in (st.get("posted") or {}).values() if x.get("result") in ("win", "loss", "push")]
        print(f"{len(ps)} graded CFB posted plays; kinds: {sorted({x.get('kind', 'ev') for x in ps})}")
        for kind in sorted({x.get("kind", "ev") for x in ps}):
            xs = [x for x in ps if x.get("kind", "ev") == kind]
            for label, sub in (("all", xs), ("went off the board", [x for x in xs if x.get("status") == "off"]),
                               ("stayed on", [x for x in xs if x.get("status") != "off"])):
                if not sub:
                    continue
                w = sum(x["result"] == "win" for x in sub); l_ = sum(x["result"] == "loss" for x in sub)
                u = sum(x.get("units") or 0 for x in sub)
                c = [x["close_ev"] for x in sub if x.get("close_ev") is not None]
                print(f"  {kind:12s} {label:20s} {len(sub):4d} plays  {w}-{l_}  {u:+.2f}u ({u / len(sub):+.1%})"
                      + (f"  avg CLV {sum(c) / len(c):+.2%}, beat close {sum(v > 0 for v in c)}/{len(c)}" if c else ""))
        by_wk = {}
        for x in ps:
            by_wk.setdefault((x.get("kind", "ev"), str(x.get("ev_key", "")).split(":")[0][:12] or "?"), []).append(x)
        return
    if a.mode == "taken":   # today's alerts and your ✅/❌ (teams and books only -- this log is public)
        now = datetime.now(timezone.utc)
        ps = sorted((x for x in fs_recent("scan_plays", "start", (now - timedelta(days=1)).isoformat())
                     if x.get("alerted") and not x.get("superseded")), key=lambda x: x.get("alerted_at") or "")
        for x in ps[-40:]:
            mark = {True: "BET ✅", False: "passed ❌", None: "-"}[x.get("taken")]
            t = datetime.fromisoformat(x["alerted_at"]).astimezone(ET).strftime("%-I:%M %p") if x.get("alerted_at") else ""
            print(f"{t:>8s}  {mark:10s} {SPORTS[x['sport']]['label']:4s} {x['label'][:34]:34s} @ {BOOK_NAMES.get(x['book'], x['book']):11s}"
                  f" {'msg' if x.get('msg_id') else 'no msg'}")
        return print(f"{sum(1 for x in ps if x.get('taken'))} marked bet, {sum(1 for x in ps if x.get('taken') is False)} passed, "
                     f"{len(ps)} alerts in the last day")
    if a.mode == "cbbbooks":
        return print(cbb_book_report())
    if a.mode == "books":
        return print(book_report(datetime.now(timezone.utc)) or "no scorecard yet")
    if a.mode == "pull":
        now = datetime.now(timezone.utc)
        st = credits_state(now)
        seen = {p["_id"]: p for p in fs_recent("scan_plays", "start", (now - timedelta(days=2)).isoformat())}
        for s in sports:
            games, plays = pull(s, now, st, "manual")
            if games is not None:
                save_fair(s, games, now.isoformat(), {})
                found = list(plays)
                for x in exchange_quotes(s, games):
                    p = evaluate(s, x["g"], x["kind"], x["side"], x["line"], x["book"], x["odds"], now, x["prob"], allow_test=True)
                    if p and not (x["size"] and x["size"] < MIN_SIZE_USD):
                        found.append((x["gid"], x["g"], p, x["size"]))
                log(f"[{s}] {record(found, seen, now)} alert(s)")
        return
    watch(a.minutes, sports)


if __name__ == "__main__":
    main()
