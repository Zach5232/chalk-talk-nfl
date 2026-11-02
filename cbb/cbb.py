"""
College basketball price plays (log-only): any sportsbook or exchange price that beats the sharp fair line.

  python3 cbb.py pull    Odds API pull for the next 18h of games (h2h + spreads + totals, 13 books incl. Pinnacle and
                         the exchanges: 6 credits however many games). Saves the fair lines, logs every starred play,
                         and refreshes the latest pre-tip fair line ("close") on plays not yet started.
  python3 cbb.py fast    Free, every ~10 min: Kalshi moneylines and the Kalshi spread ladder (every "wins by over N" rung is
                         an alt spread) vs the last saved fair line (no Odds API credits).
  python3 cbb.py grade   Finals for the last 3 days (Odds API scores, 2 credits) -> win/loss, profit, closing-line value.

Fair line = weighted consensus of every book, each devigged at its own number and moved to the line being priced with a
normal score distribution (Pinnacle 3x, exchanges 2x, others 1x), leaving out the book being checked. A starred play
must be 2%+ better than that consensus AND than Pinnacle alone -- the same bar as the CFB starred BETs.

Storage: Firestore cbb_state/fair and cbb_plays/* (no odds are written to this public repo). CBB_DRY=1 prints instead.
"""
import json, math, os, re, sys, time
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from zoneinfo import ZoneInfo

import requests

ODDS_URL = "https://api.the-odds-api.com/v4/sports/basketball_ncaab"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
PROJECT = "stock-model-42fb2"
ET = ZoneInfo("America/New_York")
DRY = os.environ.get("CBB_DRY", "").lower() in ("1", "true", "yes")

SHARP = "pinnacle"
BET_BOOKS = ["betonlineag", "espnbet", "williamhill_us", "betrivers", "betmgm", "fanduel", "draftkings",
             "ballybet", "kalshi", "polymarket", "novig", "prophetx"]
WEIGHTS = {"pinnacle": 3.0, "novig": 2.0, "prophetx": 2.0, "kalshi": 2.0, "polymarket": 2.0}
FEES = {"kalshi": ("parabolic", 0.07), "polymarket": ("parabolic", 0.07), "prophetx": ("winnings", 0.02)}
STAR_EV = 0.02          # vs consensus AND vs Pinnacle
MIN_BOOKS = 4           # books left in the consensus after removing the one being checked
MAX_MOVE = 2.5          # only move a book's price this many points to another line
SD = {"spread": 11.0, "total": 16.0}   # CBB result spread around the market number (margin, total points)
HOURS_AHEAD = 18
FAIR_MAX_AGE_H = 3      # fast mode ignores fair lines older than this (CBB lines move fast)
N = NormalDist()


# ---------------- prices ----------------
def prob(odds):
    odds = float(odds)
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def payout(odds, book=None):
    """Profit per 1 risked, after the exchange fee."""
    odds = float(odds)
    pay = odds / 100 if odds > 0 else 100 / -odds
    kind, rate = FEES.get(book, (None, 0))
    if kind == "parabolic":
        p = 1 / (1 + pay)
        return 1 / (p + rate * p * (1 - p)) - 1
    return pay * (1 - rate) if kind else pay


def american(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def mover(kind, line, q):
    """A book's devigged chance q that side A wins at `line` -> fn(new_line) for side A.
    spread: side A = home, line = home handicap (home -5 -> -5); total: side A = over; h2h: no line."""
    if kind == "h2h":
        return lambda new: q
    z = N.inv_cdf(min(max(q, 1e-4), 1 - 1e-4))
    sd = SD[kind]
    if kind == "spread":   # home margin M ~ N(mu, sd); home covers h when M > -h
        mu = sd * z - line
        return lambda new: N.cdf((mu + new) / sd)
    mu = line + sd * z     # total T ~ N(mu, sd); over L when T > L
    return lambda new: 1 - N.cdf((new - mu) / sd)


def quotes(event):
    """{kind: [{book, line, a, b}]} with a = home / over, b = away / under."""
    home, away = event["home_team"], event["away_team"]
    out = {"h2h": [], "spread": [], "total": []}
    for bm in event.get("bookmakers", []):
        for mk in bm.get("markets", []):
            o = {x["name"]: x for x in mk.get("outcomes", [])}
            if mk["key"] == "h2h" and home in o and away in o:
                out["h2h"].append(dict(book=bm["key"], line=None, a=o[home]["price"], b=o[away]["price"]))
            elif mk["key"] == "spreads" and home in o and away in o and o[home].get("point") == -o[away].get("point", 1e9):
                out["spread"].append(dict(book=bm["key"], line=o[home]["point"], a=o[home]["price"], b=o[away]["price"]))
            elif mk["key"] == "totals" and "Over" in o and "Under" in o and o["Over"].get("point") == o["Under"].get("point"):
                out["total"].append(dict(book=bm["key"], line=o["Over"]["point"], a=o["Over"]["price"], b=o["Under"]["price"]))
    for k, rows in out.items():   # drop dead quotes (beyond +/-1000) and lines far from the pack
        rows = [r for r in rows if all(100 <= abs(float(r[s])) <= 1000 for s in ("a", "b"))]
        if k != "h2h" and rows:
            mid = sorted(r["line"] for r in rows)[len(rows) // 2]
            rows = [r for r in rows if abs(r["line"] - mid) <= 3]
        out[k] = rows
    return out


def fair_fn(kind, rows):
    """fn(line, without=None, only=None) -> P(side A), or None if too few books."""
    views = [(r["book"], WEIGHTS.get(r["book"], 1.0), r["line"],
              mover(kind, r["line"], prob(r["a"]) / (prob(r["a"]) + prob(r["b"])))) for r in rows]

    def f(line, without=None, only=None):
        use = [(w, fn) for bk, w, l0, fn in views if bk != without and (only is None or bk == only)
               and (kind == "h2h" or abs(l0 - line) <= MAX_MOVE)]
        if not use or (only is None and len(use) < MIN_BOOKS):
            return None
        return sum(w * fn(line) for w, fn in use) / sum(w for w, _ in use)
    return f


def side_label(kind, side, event, line):
    home, away = event["home_team"], event["away_team"]
    if kind == "h2h":
        return f"{home if side == 'a' else away} ML"
    if kind == "spread":
        return f"{home} {line:+g}" if side == "a" else f"{away} {-line:+g}"
    return f"{'Over' if side == 'a' else 'Under'} {line:g}"


def shop(event):
    """Every starred offer in one game, plus the fair numbers saved for fast mode and closing lines."""
    q = quotes(event)
    plays, fair = [], {}
    for kind, rows in q.items():
        if not rows or not any(r["book"] == SHARP for r in rows):
            continue
        f = fair_fn(kind, rows)
        pin = next(r for r in rows if r["book"] == SHARP)
        p_pin = f(pin["line"])
        if p_pin is not None:
            fair[kind] = dict(line=pin["line"], p_a=round(p_pin, 4))
        for r in rows:
            if r["book"] == SHARP:
                continue
            pa = f(r["line"], without=r["book"])
            pp = f(r["line"], only=SHARP)
            if pa is None or pp is None:
                continue
            for side, odds, pw, ppw in (("a", r["a"], pa, pp), ("b", r["b"], 1 - pa, 1 - pp)):
                ev = pw * payout(odds, r["book"]) - (1 - pw)
                ev_pin = ppw * payout(odds, r["book"]) - (1 - ppw)
                if ev >= STAR_EV and ev_pin >= STAR_EV:
                    # "good to": the worst price that still clears the bar vs the consensus
                    good = american(1 / (1 + (1 + STAR_EV) / pw - 1)) if pw > 0 else None
                    plays.append(dict(kind=kind, side=side, line=r["line"], book=r["book"], odds=int(odds),
                                      label=side_label(kind, side, event, r["line"]), p_fair=round(pw, 4),
                                      fair_odds=american(pw), ev=round(ev, 4), ev_pin=round(ev_pin, 4), good_to=good))
    return plays, fair


# ---------------- Firestore ----------------
def _val(v):
    if v is None: return {"nullValue": None}
    if isinstance(v, bool): return {"booleanValue": v}
    if isinstance(v, int): return {"integerValue": str(v)}
    if isinstance(v, float): return {"doubleValue": v}
    if isinstance(v, dict): return {"mapValue": {"fields": {k: _val(x) for k, x in v.items()}}}
    if isinstance(v, list): return {"arrayValue": {"values": [_val(x) for x in v]}}
    return {"stringValue": str(v)}


def _unval(v):
    if "mapValue" in v: return {k: _unval(x) for k, x in v["mapValue"].get("fields", {}).items()}
    if "arrayValue" in v: return [_unval(x) for x in v["arrayValue"].get("values", [])]
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue" in v: return v["doubleValue"]
    if "booleanValue" in v: return v["booleanValue"]
    if "nullValue" in v: return None
    return v.get("stringValue", v.get("timestampValue"))


_TOKEN = None


def token():
    global _TOKEN
    if _TOKEN is None:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        info = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
        creds = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/datastore"])
        creds.refresh(Request())
        _TOKEN = creds.token
    return _TOKEN


def doc_url(path):
    return f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents/{path}"


DRY_DIR = os.environ.get("CBB_DRY_DIR", "/tmp/cbb_dry")


def fs_get(path):
    if DRY:  # dry runs keep state in local files so pull -> fast can be tested end to end
        f = os.path.join(DRY_DIR, path.replace("/", "__") + ".json")
        return json.load(open(f)) if os.path.exists(f) else None
    r = requests.get(doc_url(path), headers={"Authorization": f"Bearer {token()}"}, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return {k: _unval(v) for k, v in r.json().get("fields", {}).items()}


def fs_set(path, data):
    if DRY:
        os.makedirs(DRY_DIR, exist_ok=True)
        json.dump(data, open(os.path.join(DRY_DIR, path.replace("/", "__") + ".json"), "w"), default=str)
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
        return []
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
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", f"{event_id}_{p['kind']}_{p['side']}_{line}_{p['book']}")


# ---------------- modes ----------------
def pull():
    now = datetime.now(timezone.utc)
    params = dict(apiKey=os.environ["ODDS_API_KEY"], bookmakers=",".join([SHARP] + BET_BOOKS),
                  markets="h2h,spreads,totals", oddsFormat="american",
                  commenceTimeFrom=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  commenceTimeTo=(now + timedelta(hours=HOURS_AHEAD)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    r = requests.get(f"{ODDS_URL}/odds", params=params, timeout=60)
    r.raise_for_status()
    events = r.json()
    print(f"odds pull: {len(events)} games, {r.headers.get('x-requests-last')} credits "
          f"({r.headers.get('x-requests-remaining')} left this month)")
    fair_doc = fs_get("cbb_state/fair") or {}
    games = {k: v for k, v in (fair_doc.get("games") or {}).items() if v.get("tip", "") > (now - timedelta(hours=6)).isoformat()}
    new = 0
    open_plays = [p for p in fs_list("cbb_plays") if p.get("tip", "") > now.isoformat() and not p.get("result")]
    by_event = {}
    for p in open_plays:
        by_event.setdefault(p["event_id"], []).append(p)
    for ev in events:
        plays, fair = shop(ev)
        if fair:
            games[ev["id"]] = dict(home=ev["home_team"], away=ev["away_team"], tip=ev["commence_time"], fair=fair,
                                   at=now.isoformat())
        for p in plays:
            pid = play_id(p, ev["id"])
            if any(x["_id"] == pid for x in by_event.get(ev["id"], [])):
                continue
            new += 1
            fs_set(f"cbb_plays/{pid}", dict(**p, event_id=ev["id"], home=ev["home_team"], away=ev["away_team"],
                                            tip=ev["commence_time"], found=now.isoformat(), source="pull"))
            print(f"  ★ {ev['away_team']} @ {ev['home_team']}: {p['label']} {p['odds']:+d} @ {p['book']} "
                  f"(fair {p['fair_odds']:+d}, EV {p['ev']:+.1%}, Pinnacle {p['ev_pin']:+.1%}, good to {p['good_to']:+d})")
        # closing line: the latest fair price for every logged play in this game that hasn't tipped
        q = quotes(ev)
        for p in by_event.get(ev["id"], []):
            rows = q.get(p["kind"]) or []
            if not rows:
                continue
            pa = fair_fn(p["kind"], rows)(p["line"])
            if pa is not None:
                pw = pa if p["side"] == "a" else 1 - pa
                fs_set(f"cbb_plays/{p['_id']}", {**{k: v for k, v in p.items() if k != "_id"},
                                                 "p_close": round(pw, 4), "close_at": now.isoformat()})
    fs_set("cbb_state/fair", dict(games=games, updated=now.isoformat()))
    print(f"logged {new} new play(s); fair lines saved for {len(games)} games")


def norm(name):
    """Team name key shared by the Odds API and Kalshi: 'Iowa St.', 'Iowa State Cyclones' and 'Iowa St Cyclones'
    all start with 'iowast'; Kalshi's short name is matched as a prefix of the Odds API's full name."""
    n = name.lower().replace("&", "and").replace("'", "").replace("é", "e")
    n = re.sub(r"\bstate\b", "st", n)
    return re.sub(r"[^a-z]", "", n)


def kalshi_markets(series):
    ms, cursor = [], None
    for _ in range(30):
        params = dict(series_ticker=series, status="open", limit=1000, **({"cursor": cursor} if cursor else {}))
        d = requests.get(f"{KALSHI}/markets", params=params, headers={"User-Agent": "cbb-model"}, timeout=30).json()
        ms += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor or not d.get("markets"):
            break
        time.sleep(0.2)  # Kalshi rate-limits bursts
    return ms


def match_team(name, games):
    """(game id, game, 'a' if the team is home else 'b') for a Kalshi team name, or None if not exactly one match."""
    n = norm(name)
    hits = [(gid, g, "a" if norm(g["home"]).startswith(n) else "b") for gid, g in games.items()
            if n and (norm(g["home"]).startswith(n) != norm(g["away"]).startswith(n))]
    return hits[0] if len(hits) == 1 else None


LADDER_MAX_MOVE = 6.0   # price ladder rungs this many points either side of the fair spread at most


def ladder(fresh, seen, now):
    """Kalshi spread ladder ('Duke wins by over 7.5 points'): every rung is an alt spread. Yes = that team -N,
    No = the other team +N. Priced off the saved fair spread moved along the margin distribution."""
    ms = kalshi_markets("KXNCAAMBSPREAD")
    games = {gid: g for gid, g in fresh.items() if "spread" in g.get("fair", {})}
    new = rungs = 0
    for m in ms:
        t = re.match(r"^(.*) wins by over ([\d.]+) points?", m.get("title") or "")
        if not t:
            continue
        hit = match_team(t.group(1), games)
        if not hit:
            continue
        gid, g, team_side = hit
        n = float(t.group(2))
        fs = g["fair"]["spread"]
        home_cover = mover("spread", fs["line"], fs["p_a"])      # P(home covers at home handicap h)
        # (side, home handicap) for Yes (team -n) and No (other team +n)
        yes = ("a", -n) if team_side == "a" else ("b", n)
        no = ("b", -n) if team_side == "a" else ("a", n)
        yb, ya = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
        for (side, line), ask, size in ((yes, ya, float(m.get("yes_ask_size_fp") or 0)),
                                        (no, 1 - yb if yb > 0 else 0, float(m.get("yes_bid_size_fp") or 0))):
            if not 0.02 < ask < 0.98 or abs(line - fs["line"]) > LADDER_MAX_MOVE or size * ask < 20:
                continue
            rungs += 1
            pa = home_cover(line)
            pw = pa if side == "a" else 1 - pa
            odds = american(ask)
            ev = pw * payout(odds, "kalshi") - (1 - pw)
            if ev < STAR_EV:
                continue
            p = dict(kind="spread", side=side, line=line, book="kalshi", odds=odds,
                     label=side_label("spread", side, {"home_team": g["home"], "away_team": g["away"]}, line),
                     p_fair=round(pw, 4), fair_odds=american(pw), ev=round(ev, 4), ev_pin=None,
                     good_to=american(pw / (1 + STAR_EV)))
            pid = play_id(p, gid)
            if pid in seen:
                continue
            seen.add(pid); new += 1
            fs_set(f"cbb_plays/{pid}", dict(**p, event_id=gid, home=g["home"], away=g["away"], tip=g["tip"],
                                            found=now.isoformat(), source="ladder", size_usd=round(size * ask),
                                            fair_at=g["at"], fair_line=fs["line"]))
            print(f"  ★ Kalshi ladder {p['label']} {odds:+d} (${size * ask:,.0f} avail) fair {p['fair_odds']:+d} EV {ev:+.1%}")
    print(f"kalshi ladder: {len(ms)} rungs listed, {rungs} priced, {new} new play(s)")


def fast():
    """Kalshi moneyline asks vs the last saved fair line. Free; meant to run every ~10 minutes."""
    now = datetime.now(timezone.utc)
    fair_doc = fs_get("cbb_state/fair") or {}
    games = fair_doc.get("games") or {}
    fresh = {k: g for k, g in games.items() if "h2h" in g.get("fair", {}) and g["tip"] > now.isoformat()
             and (now - datetime.fromisoformat(g["at"])).total_seconds() < FAIR_MAX_AGE_H * 3600}
    if not fresh:
        print("no fresh fair lines (run 'pull' first)"); return
    ms = kalshi_markets("KXNCAAMBGAME")
    events = {}
    for m in ms:
        events.setdefault(m["event_ticker"], []).append(m)
    seen = {p["_id"] for p in fs_list("cbb_plays")}
    new = matched = 0
    for et, mk in events.items():
        if len(mk) != 2:
            continue
        names = [norm(m.get("yes_sub_title") or "") for m in mk]
        for gid, g in fresh.items():
            h, a = norm(g["home"]), norm(g["away"])
            side = {}
            for m, n in zip(mk, names):
                if n and h.startswith(n) and not a.startswith(n):
                    side["a"] = m
                elif n and a.startswith(n) and not h.startswith(n):
                    side["b"] = m
            if len(side) != 2:
                continue
            matched += 1
            p_home = g["fair"]["h2h"]["p_a"]
            for s, m in side.items():
                ask = float(m.get("yes_ask_dollars") or 0)
                size = float(m.get("yes_ask_size_fp") or 0) * ask
                if not 0 < ask < 1 or size < 20:
                    continue
                pw = p_home if s == "a" else 1 - p_home
                odds = american(ask)
                ev = pw * payout(odds, "kalshi") - (1 - pw)
                if ev < STAR_EV:
                    continue
                p = dict(kind="h2h", side=s, line=None, book="kalshi", odds=odds,
                         label=f"{g['home'] if s == 'a' else g['away']} ML", p_fair=round(pw, 4), fair_odds=american(pw),
                         ev=round(ev, 4), ev_pin=None, good_to=american(1 / (1 + (1 + STAR_EV) / pw - 1)))
                pid = play_id(p, gid)
                if pid in seen:
                    continue
                new += 1
                fs_set(f"cbb_plays/{pid}", dict(**p, event_id=gid, home=g["home"], away=g["away"], tip=g["tip"],
                                                found=now.isoformat(), source="fast", size_usd=round(size), fair_at=g["at"]))
                print(f"  ★ Kalshi {p['label']} {odds:+d} (${size:,.0f} avail) fair {p['fair_odds']:+d} EV {ev:+.1%}")
            break
    print(f"kalshi: {len(events)} games listed, {matched} matched to fair lines, {new} new play(s)")
    ladder(fresh, seen, now)


def grade():
    now = datetime.now(timezone.utc)
    r = requests.get(f"{ODDS_URL}/scores", params=dict(apiKey=os.environ["ODDS_API_KEY"], daysFrom=3), timeout=60)
    r.raise_for_status()
    finals = {}
    for g in r.json():
        if g.get("completed") and g.get("scores"):
            s = {x["name"]: float(x["score"]) for x in g["scores"]}
            finals[g["id"]] = (s.get(g["home_team"]), s.get(g["away_team"]))
    plays = fs_list("cbb_plays")
    done = 0
    for p in plays:
        if p.get("result") or p["event_id"] not in finals:
            continue
        hs, as_ = finals[p["event_id"]]
        if hs is None or as_ is None:
            continue
        if p["kind"] == "h2h":
            x = (hs - as_) if p["side"] == "a" else (as_ - hs)
        elif p["kind"] == "spread":
            x = (hs - as_ + p["line"]) if p["side"] == "a" else -(hs - as_ + p["line"])
        else:
            x = (hs + as_ - p["line"]) if p["side"] == "a" else -(hs + as_ - p["line"])
        res = "win" if x > 0 else "loss" if x < 0 else "push"
        profit = payout(p["odds"], p["book"]) if res == "win" else -1.0 if res == "loss" else 0.0
        pc = p.get("p_close")
        clv = round(pc * payout(p["odds"], p["book"]) - (1 - pc), 4) if pc is not None else None
        fs_set(f"cbb_plays/{p['_id']}", {**{k: v for k, v in p.items() if k != "_id"}, "result": res,
                                         "profit": round(profit, 4), "ev_close": clv, "final": f"{as_:g}-{hs:g}"})
        done += 1
    graded = [p for p in fs_list("cbb_plays") if p.get("result")] if not DRY else []
    if graded:
        n = len(graded); prof = sum(p["profit"] for p in graded)
        cl = [p["ev_close"] for p in graded if p.get("ev_close") is not None]
        beat = sum(c > 0 for c in cl)
        print(f"graded {done} new. Season: {n} plays, {prof:+.1f}u ({prof / n:+.1%}), "
              f"beat the close {beat}/{len(cl)}, avg value vs close {sum(cl) / max(len(cl), 1):+.1%}")
    else:
        print(f"graded {done} new")


if __name__ == "__main__":
    {"pull": pull, "fast": fast, "grade": grade}[sys.argv[1]]()
