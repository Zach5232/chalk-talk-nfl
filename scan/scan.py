"""
Value scanner: NFL, NBA, college basketball and NHL game lines (moneyline, spread, total) at every book and exchange
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
import argparse, json, math, os, re, time
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
    "nhl":   dict(odds="icehockey_nhl", novig="NHL", poly=["nhl"], label="NHL",
                  kalshi=dict(h2h="KXNHLGAME", spread="KXNHLSPREAD", total="KXNHLTOTAL"),
                  sd=dict(spread=2.3, total=2.3), max_move=0.0),
}
SHARP = "pinnacle"
# 10 named books = 1 region of credits per market. Fanatics / Bovada left out on purpose (can't bet there).
BOOKS = ["pinnacle", "novig", "prophetx", "kalshi", "polymarket", "fanduel", "betrivers", "betonlineag", "espnbet", "betmgm"]
WEIGHTS = {"pinnacle": 3.0, "novig": 2.0, "prophetx": 2.0, "kalshi": 2.0, "polymarket": 2.0}
# pregame taker fees, checked on real slips (see nhl-model model_config.json exchange_fees)
FEES = {"kalshi": ("parabolic", 0.07), "polymarket": ("parabolic", 0.07), "prophetx": ("winnings", 0.02)}
FEES_BY_SPORT = {("nhl", "prophetx"): ("parabolic", 0.035)}
BAR = 0.03                 # EV after fees vs consensus and vs Pinnacle
BAR_STRICT = 0.04          # Kalshi / Polymarket, and plays found more than EARLY_H before start
STRICT_BOOKS = {"kalshi", "polymarket"}
EARLY_H = 6
MAX_EV = 0.12              # above this it's a stale quote or a mismatched game, not value
MIN_BOOKS = 3              # books left in the consensus after removing the one being checked
MIN_SIZE_USD = 25          # exchange liquidity at the quoted price
REPOST_CENTS = 10          # alert the same play again only if the price improved this much
BANKROLL = float(os.environ.get("SCAN_BANKROLL", "2000"))
KELLY = 1 / 8
MAX_STAKE = 0.02
HOURS_AHEAD = 24
CREDIT_CAP = int(os.environ.get("SCAN_CREDIT_CAP", "3000"))
CYCLE_S = 180
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


def quotes(event):
    """{kind: [{book, line, a, b}]}, a = home / over, b = away / under."""
    home, away = event["home_team"], event["away_team"]
    out = {"h2h": [], "spread": [], "total": []}
    for bm in event.get("bookmakers", []):
        for mk in bm.get("markets", []):
            o = {x["name"]: x for x in mk.get("outcomes", [])}
            if mk["key"] == "h2h" and home in o and away in o and len(o) == 2:
                out["h2h"].append(dict(book=bm["key"], line=None, a=o[home]["price"], b=o[away]["price"]))
            elif mk["key"] == "spreads" and home in o and away in o and o[home].get("point") == -o[away].get("point", 1e9):
                out["spread"].append(dict(book=bm["key"], line=o[home]["point"], a=o[home]["price"], b=o[away]["price"]))
            elif mk["key"] == "totals" and "Over" in o and "Under" in o and o["Over"].get("point") == o["Under"].get("point"):
                out["total"].append(dict(book=bm["key"], line=o["Over"]["point"], a=o["Over"]["price"], b=o["Under"]["price"]))
    for k, rows in out.items():   # drop dead quotes and lines far from the pack
        rows = [r for r in rows if all(100 <= abs(float(r[s])) <= 1500 for s in ("a", "b"))]
        if k != "h2h" and rows:
            mid = sorted(r["line"] for r in rows)[len(rows) // 2]
            rows = [r for r in rows if abs(r["line"] - mid) <= 3]
        out[k] = rows
    return out


def fair_views(sport, kind, rows):
    """Compact per-book views saved with each game: [book, weight, line, devigged P(side A)]."""
    return [[r["book"], WEIGHTS.get(r["book"], 1.0), r["line"], round(prob(r["a"]) / (prob(r["a"]) + prob(r["b"])), 5)]
            for r in rows]


def fair_at(sport, kind, views, line, without=None, only=None):
    """Consensus P(side A) at `line` from saved views, or None if too few books."""
    mm = SPORTS[sport]["max_move"]
    use = [(w, mover(sport, kind, l0, q)) for bk, w, l0, q in views
           if bk != without and (only is None or bk == only) and (kind == "h2h" or abs(l0 - line) <= mm + 1e-9)]
    if not use or (only is None and len(use) < MIN_BOOKS):
        return None
    return sum(w * fn(line) for w, fn in use) / sum(w for w, _ in use)


def bar_for(book, hours_to_start):
    return BAR_STRICT if book in STRICT_BOOKS or hours_to_start > EARLY_H else BAR


def evaluate(sport, g, kind, side, line, book, odds, now, price_prob=None):
    """A play dict if (book, odds) on this side clears the bar, else None. g = saved game (with views)."""
    views = (g.get("views") or {}).get(kind)
    if not views:
        return None
    pa = fair_at(sport, kind, views, line, without=book)
    pp = fair_at(sport, kind, views, line, only=SHARP)
    if pa is None or pp is None:
        return None
    pw, ppw = (pa, pp) if side == "a" else (1 - pa, 1 - pp)
    pay = payout(odds, book, sport)
    ev, ev_pin = pw * pay - (1 - pw), ppw * pay - (1 - ppw)
    hrs = (datetime.fromisoformat(g["start"]) - now).total_seconds() / 3600
    if hrs * 60 < START_BUFFER_MIN:
        return None
    bar = bar_for(book, hrs)
    if not (bar <= ev <= MAX_EV and ev_pin >= bar):
        return None
    kelly = ev / pay
    stake = min(kelly * KELLY, MAX_STAKE)
    # worst price that still clears the bar vs the consensus (fees ignored for the display number)
    good = american(pw / (1 + bar))
    return dict(sport=sport, kind=kind, side=side, line=line, book=book, odds=int(odds), label=label(g, kind, side, line),
                p_fair=round(pw, 4), fair_odds=american(pw), ev=round(ev, 4), ev_pin=round(ev_pin, 4),
                stake_pct=round(stake, 4), stake_usd=round(stake * BANKROLL), good_to=good, bar=bar,
                price_prob=round(price_prob if price_prob is not None else prob(odds), 4))


def label(g, kind, side, line):
    if kind == "h2h":
        return f"{g['home'] if side == 'a' else g['away']} ML"
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
    params = dict(apiKey=key, bookmakers=",".join(BOOKS), markets="h2h,spreads,totals", oddsFormat="american",
                  commenceTimeFrom=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  commenceTimeTo=(now + timedelta(hours=HOURS_AHEAD)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    r = requests.get(f"{ODDS}/{SPORTS[sport]['odds']}/odds", params=params, timeout=60)
    if r.status_code == 422 or r.status_code == 404:
        log(f"[{sport}] odds api {r.status_code} (off-season?)")
        return {}, []
    r.raise_for_status()
    cost = int(r.headers.get("x-requests-last") or 3)
    charge(st, now, cost)
    events = r.json()
    games, plays = {}, []
    for ev in events:
        q = quotes(ev)
        if not any(r_["book"] == SHARP for rows in q.values() for r_ in rows):
            continue
        g = dict(home=ev["home_team"], away=ev["away_team"], start=ev["commence_time"].replace("Z", "+00:00"),
                 at=now.isoformat(), views={k: fair_views(sport, k, rows) for k, rows in q.items() if rows})
        games[ev["id"]] = g
        for kind, rows in q.items():
            for row in rows:
                if row["book"] == SHARP:
                    continue
                for side, odds in (("a", row["a"]), ("b", row["b"])):
                    p = evaluate(sport, g, kind, side, row["line"], row["book"], odds, now)
                    if p:
                        plays.append((ev["id"], g, p, None))
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


def find_game(games, t1, t2, day=None, start=None):
    """(gid, game, t1_is_home) for two exchange team names, if exactly one saved game has both teams."""
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


def novig_quotes(sport, games):
    lg = SPORTS[sport]["novig"]
    ev_names = {}
    for e in (_get(f"{NOVIG}/catalog/events", dict(league=lg)) or {}).get("items", []):
        mt = re.match(r"(.+?)\s+@\s+(.+)", e.get("description") or "")
        if mt:
            ev_names[e["eventId"]] = (mt.group(1), mt.group(2))   # (away, home)
    markets = []
    for mtype in ("MONEY", "SPREAD", "TOTAL"):
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
        d = _get(f"{KALSHI}/markets", dict(series_ticker=series, status="open", limit=1000, **({"cursor": cursor} if cursor else {})))
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
               "nba", "ncaa", "regular", "season"}


def _rules_teams(m):
    """'If Utah wins the Denver vs Utah Pro Basketball game originally scheduled ...' -> ('Denver', 'Utah')."""
    t = re.search(r"\bthe ((?:(?!\bthe\b).)+?) (?:vs\.?|at|@) (.+?) game\b", m.get("rules_primary") or "")
    if not t:
        return None
    b = t.group(2).split()
    while b and b[-1].lower() in SPORT_WORDS:
        b.pop()
    return (t.group(1), " ".join(b)) if b else None


def kalshi_quotes(sport, games):
    out = []
    for kind, series in SPORTS[sport]["kalshi"].items():
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


def poly_quotes(sport, games):
    sports = _get(f"{GAMMA}/sports") or []
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
                if typ not in ("moneyline", "spreads", "totals") or not m.get("acceptingOrders", True):
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
                teams = re.match(r"(.+?) vs\.? (.+?)(?::|$)", ev.get("title") or "")
                if not teams:
                    continue
                hit = find_game(games, teams.group(1), teams.group(2), start=start)
                if not hit:
                    continue
                gid, g, first_is_home = hit
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


def exchange_quotes(sport, games):
    """Pregame prices only: games past (or within minutes of) their start are dropped, by our start time or the venue's."""
    cut = datetime.now(timezone.utc) + timedelta(minutes=START_BUFFER_MIN)
    games = {k: g for k, g in games.items() if datetime.fromisoformat(g["start"]) > cut}
    res = []
    for name, fn in (("novig", novig_quotes), ("kalshi", kalshi_quotes), ("polymarket", poly_quotes)):
        try:
            res += fn(sport, games)
        except Exception as e:  # noqa: BLE001 -- one venue down never stops the scan
            log(f"[{sport}] {name}: {type(e).__name__}: {str(e)[:120]}")
    return [q for q in res if not q.get("start") or q["start"] > cut]


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


def discord_send(text, edit_id=None):
    """Post one alert (returns its message id), or edit an earlier one in place (no new notification)."""
    if DRY:
        print("   [edit]" if edit_id else "   [alert]", text.replace("\n", " | "))
        return edit_id or f"dry-{abs(hash(text)) % 10**8}"
    url = webhook()
    if not POST or not url:   # public log: never print prices
        log("alert not posted (posting off or no webhook)")
        return None
    try:
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


BOOK_NAMES = {"novig": "Novig", "prophetx": "ProphetX", "kalshi": "Kalshi", "polymarket": "Polymarket",
              "fanduel": "FanDuel", "betrivers": "BetRivers", "betonlineag": "BetOnline", "espnbet": "ESPN Bet",
              "betmgm": "BetMGM"}


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
    txt = (f"🔎 **{SPORTS[p['sport']]['label']}** {g['away']} @ {g['home']} · {t} ET\n"
           f"**{p['label']} · {price_str(p['odds'], p['book'], p.get('price_prob'))} @ {bk}**\n"
           f"Fair {_pct(p['p_fair'])}% ({p['fair_odds']:+d}) · EV {p['ev']:+.1%} · bet ${p['stake_usd']} (1/8 Kelly){good}{size}")
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


def relation(p, q, g):
    """p and q on opposite sides of the same game: ('middle' | 'arb' | 'conflict', text) or None if not opposed."""
    ax1, d1, t1 = region(p)
    ax2, d2, t2 = region(q)
    if ax1 != ax2 or d1 == d2:
        return None
    over, under = (p, q) if d1 == ">" else (q, p)
    lo, hi = region(over)[2], region(under)[2]
    if lo < hi:
        return "middle", f"both win if {_bounds(ax1, lo, hi, g)}"
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
        return f"🎯 **Middle** with {ref}: {text} -- fine to hold both"
    if kind == "arb":
        return f"💰 **Arb** with {ref}: {text}"
    return (f"⚠️ **Opposite side** of {ref}: {text}. "
            + ("This one is the better value -- if you haven't bet the other, take this one only."
               if better else "The other one is the better value -- take only one."))


def record(found, seen, now):
    """Save new plays. A game/market/side is posted once; a better number later (same line 10+ cents better, or
    another line with 2%+ more EV) edits that same Discord message instead of posting again."""
    best = {}
    for gid, g, p, size in found:   # one play per game / market / side: the best EV across books and alt lines
        k = (p["sport"], gid, p["kind"], p["side"])
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
        base = dict(**p, event_id=gid, home=g["home"], away=g["away"], start=g["start"], fair_at=g["at"], **extra)
        # plays already posted on the other side of this game
        notes, edits, skip = [], [], False
        for o in [x for x in seen.values() if x.get("alerted") and not x.get("superseded") and x["event_id"] == gid
                  and x["sport"] == p["sport"] and x["kind"] in ("h2h", "spread", "total")]:
            rel = relation(p, o, g)
            if not rel:
                continue
            kind, text = rel
            if kind == "conflict" and p["ev"] <= o["ev"]:
                skip = True   # the play already posted is the better value: don't post the weaker opposite
                break
            notes.append(_note_for(kind, text, o, better=True))
            edits.append((o, _note_for(kind, text, {**p, **extra}, better=False) if kind == "conflict"
                          else _note_for(kind, text, {**p, **extra}, better=True)))
        if skip:
            if not prev:
                fs_set(f"scan_plays/{pid}", dict(base, found=now.isoformat(), alerted=False, conflict=True))
                seen[pid] = dict(base, found=now.isoformat(), alerted=False, conflict=True)
            continue
        note = ("\n" + "\n".join(notes)) if notes else ""
        for o, onote in edits:   # tell the earlier message about the new one
            if o.get("msg_id"):
                first = seen.get(o.get("first_id")) if o.get("updated") else None
                discord_send(alert_text(o, o, first=first) + "\n" + onote, edit_id=o["msg_id"])
        if not sib:
            mid = discord_send(alert_text({**p, **extra}, g) + note)
            doc = dict(base, found=prev["found"] if prev else now.isoformat(), alerted=True, odds_alerted=p["odds"],
                       alerted_at=now.isoformat(), msg_id=mid, first_id=pid)
            fs_set(f"scan_plays/{pid}", doc)
            seen[pid] = doc
            posted += 1
            continue
        last = sib[-1]
        same_line = last["line"] == p["line"]
        better = (same_line and payout(p["odds"], "x", p["sport"]) * 100
                  >= payout(last.get("odds_alerted") or last["odds"], "x", p["sport"]) * 100 + REPOST_CENTS) \
            or (not same_line and p["ev"] >= last["ev"] + 0.02)
        if not better:
            if not prev:
                fs_set(f"scan_plays/{pid}", dict(base, found=now.isoformat(), alerted=False))
                seen[pid] = dict(base, found=now.isoformat(), alerted=False)
            continue
        first = seen.get(last.get("first_id")) or last
        mid = discord_send(alert_text({**p, **extra}, g, first=first) + note, edit_id=last.get("msg_id"))
        if mid is None and last.get("msg_id"):
            mid = last["msg_id"]
        # the older doc stays for the record but is graded as superseded; the bettor took one of them
        fs_set(f"scan_plays/{last['_id'] if last.get('_id') else play_id(last, last['event_id'])}",
               {**{k: v for k, v in last.items() if k != "_id"}, "superseded": True})
        last["superseded"] = True
        doc = dict(base, found=prev["found"] if prev else now.isoformat(), alerted=True, odds_alerted=p["odds"],
                   alerted_at=now.isoformat(), msg_id=mid, first_id=last.get("first_id") or pid, updated=True)
        fs_set(f"scan_plays/{pid}", doc)
        seen[pid] = doc
        posted += 1
    return posted


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
        pa = fair_at(sport, p["kind"], views, p["line"])
        pp = fair_at(sport, p["kind"], views, p["line"], only=SHARP)
        if pa is None:
            continue
        side = (lambda x: x) if p["side"] == "a" else (lambda x: 1 - x)
        doc = {k: v for k, v in p.items() if k != "_id"}
        doc.update(p_close=round(side(pa), 4), p_close_pin=round(side(pp), 4) if pp is not None else None,
                   close_at=now.isoformat())
        fs_set(f"scan_plays/{p.get('_id') or play_id(p, p['event_id'])}", doc)
        p.update(doc)
        n += 1
    return n


def watch(minutes, sports):
    t_end = time.time() + minutes * 60
    seen = {p["_id"]: p for p in fs_list("scan_plays")}
    state = {}
    for s in sports:
        games, pulled = load_fair(s)
        state[s] = dict(games=games, pulled=pulled, mids={})
    cycle = 0
    while time.time() < t_end:
        cycle += 1
        now = datetime.now(timezone.utc)
        st = credits_state(now)
        n_alerts = n_quotes = 0
        for s in sports:
            S = state[s]
            S["games"] = {k: g for k, g in S["games"].items() if datetime.fromisoformat(g["start"]) > now - timedelta(hours=8)}
            upcoming = {k: g for k, g in S["games"].items() if now < datetime.fromisoformat(g["start"]) < now + timedelta(hours=12)}
            pulled = datetime.fromisoformat(S["pulled"]) if S["pulled"] else None
            age_min = (now - pulled).total_seconds() / 60 if pulled else 1e9
            reasons = []
            if (age_min >= BASE_GAP_MIN and upcoming) or age_min >= 12 * 60:
                reasons.append("baseline")
            open_plays = [p for p in seen.values() if p.get("sport") == s and p.get("alerted") and not p.get("superseded")
                          and p.get("p_close") is None
                          and now < datetime.fromisoformat(p["start"]) < now + timedelta(minutes=CLOSE_WINDOW_MIN)]
            if open_plays and age_min >= 8:
                reasons.append("closing line")
            q = exchange_quotes(s, S["games"]) if S["games"] else []
            n_quotes += len(q)
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
            # exchange prices that clear the bar
            found, stale = [], []
            for x in q:
                if x["size"] and x["size"] < MIN_SIZE_USD:
                    continue
                p = evaluate(s, x["g"], x["kind"], x["side"], x["line"], x["book"], x["odds"], now, x["prob"])
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
            if reasons and (age_min >= MIN_PULL_GAP_MIN or "closing line" in reasons):
                games, plays = pull(s, now, st, ", ".join(reasons))
                if games is not None:
                    S["games"], S["pulled"], S["mids"] = games, now.isoformat(), {}
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
                        p = evaluate(s, x["g"], x["kind"], x["side"], x["line"], x["book"], x["odds"], now, x["prob"])
                        if p:
                            found.append((x["gid"], x["g"], p, x["size"]))
                    n_quotes += len(q)
                    close_lines(s, S["games"], list(seen.values()), now)
                    save_fair(s, S["games"], S["pulled"], S["mids"])
            if found:
                n_alerts += record(found, seen, now)
        log(f"cycle {cycle}: {n_quotes} exchange prices checked, {n_alerts} alert(s); credits {st['used']}/{CREDIT_CAP}")
        time.sleep(max(0, min(CYCLE_S, t_end - time.time())))


# ---------------- grading ----------------
ESPN = {"nfl": "football/nfl", "nba": "basketball/nba", "ncaab": "basketball/mens-college-basketball", "nhl": "hockey/nhl"}


def espn_finals(sport, day):
    params = dict(dates=day.replace("-", ""), limit=500)
    if sport == "ncaab":
        params["groups"] = 50
    d = _get(f"https://site.api.espn.com/apis/site/v2/sports/{ESPN[sport]}/scoreboard", params) or {}
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


def grade():
    now = datetime.now(timezone.utc)
    plays = [p for p in fs_list("scan_plays") if not p.get("result")
             and datetime.fromisoformat(p["start"]) < now - timedelta(hours=4)]
    cache, done = {}, 0
    for p in plays:
        day = datetime.fromisoformat(p["start"]).astimezone(ET).strftime("%Y-%m-%d")
        key = (p["sport"], day)
        if key not in cache:
            cache[key] = espn_finals(*key)
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
        if p["kind"] == "h2h":
            m = f["hs"] - f["as_"]
            res = "push" if m == 0 else ("win" if (m > 0) == (p["side"] == "a") else "loss")
        elif p["kind"] == "spread":
            m = f["hs"] - f["as_"] + p["line"]
            res = "push" if m == 0 else ("win" if (m > 0) == (p["side"] == "a") else "loss")
        else:
            m = f["hs"] + f["as_"] - p["line"]
            res = "push" if m == 0 else ("win" if (m > 0) == (p["side"] == "a") else "loss")
        pay = payout(p["odds"], p["book"], p["sport"])
        pl = {"win": pay, "loss": -1.0, "push": 0.0}[res]
        doc = {k: v for k, v in p.items() if k != "_id"}
        doc.update(result=res, pl_unit=round(pl, 4), pl_usd=round(pl * (p.get("stake_usd") or 0), 2),
                   clv=round(p["p_close"] * (1 + pay) - 1, 4) if p.get("p_close") is not None else None,
                   clv_pin=round(p["p_close_pin"] * (1 + pay) - 1, 4) if p.get("p_close_pin") is not None else None,
                   graded_at=now.isoformat())
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["watch", "pull", "grade"])
    ap.add_argument("sport", nargs="?", default="")
    ap.add_argument("--minutes", type=float, default=330)
    ap.add_argument("--sports", default=os.environ.get("SCAN_SPORTS", "nfl,nba,ncaab,nhl"))
    a = ap.parse_args()
    sports = [s for s in (a.sport or a.sports).split(",") if s in SPORTS]
    if a.mode == "grade":
        return grade()
    if a.mode == "pull":
        now = datetime.now(timezone.utc)
        st = credits_state(now)
        seen = {p["_id"]: p for p in fs_list("scan_plays")}
        for s in sports:
            games, plays = pull(s, now, st, "manual")
            if games is not None:
                save_fair(s, games, now.isoformat(), {})
                found = list(plays)
                for x in exchange_quotes(s, games):
                    p = evaluate(s, x["g"], x["kind"], x["side"], x["line"], x["book"], x["odds"], now, x["prob"])
                    if p and not (x["size"] and x["size"] < MIN_SIZE_USD):
                        found.append((x["gid"], x["g"], p, x["size"]))
                log(f"[{s}] {record(found, seen, now)} alert(s)")
        return
    watch(a.minutes, sports)


if __name__ == "__main__":
    main()
