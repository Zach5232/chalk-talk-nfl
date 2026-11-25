"""Futures: a daily board of every season-long market we can price, plus value alerts, in Discord #futures.

Sources
  * Kalshi (free): winners, conference/division winners, make-the-playoffs, CFP rounds, March Madness rounds.
  * Odds API outrights (about 4 credits a market a day): Super Bowl, NBA, Stanley Cup, CFB and March Madness
    champions, golf majors -- every book, incl. Pinnacle and Betfair where they price it.

Fair chance per team / player
  * Winner markets: each book de-vigged across the whole field (power method), weighted Pinnacle / Betfair 3x,
    LowVig 1.5x, other books 1x, plus Kalshi's bid/ask midpoint at 2x (1x if its spread is wide); renormalized.
  * Markets with no sharp book (CFB / March Madness champions): Kalshi is the anchor (owner's call, Nov 25).
  * Props that only Kalshi lists (divisions, make-the-playoffs, rounds): Kalshi midpoint -- shown on the board for
    watching; no alert, since there is nothing independent to compare against yet.

Alerts (bar 5%, 8% under a 3% fair chance), one message per team/player per market, edited in place when a
clearly better number appears (2%+ more EV). Stake shown: 1/16 Kelly. Bet it can be tapped more than once -- each
bet is kept with its own price and book. Board: one pinned message per sport, edited daily, with each pick's fair
chance, its 7-day move, and the best price. Daily snapshots in Firestore (scan_futures) build the movement history.
Counts only in the public log.
"""
import re
import time
from datetime import datetime, timedelta, timezone

import requests

import scan as S

KAL = S.KALSHI
SPORT_LABEL = {"nfl": "🏈 NFL", "nba": "🏀 NBA", "nhl": "🏒 NHL", "ncaaf": "🏈 College football",
               "ncaab": "🏀 College basketball", "golf": "⛳ Golf"}

# key, label, sport, Kalshi series (or None), Odds API outright key (or None)
MARKETS = [
    ("sb", "Super Bowl", "nfl", "KXSB", "americanfootball_nfl_super_bowl_winner"),
    ("afc", "AFC champion", "nfl", "KXNFLAFCCHAMP", None),
    ("nfc", "NFC champion", "nfl", "KXNFLNFCCHAMP", None),
    ("nflpo", "Make the NFL playoffs", "nfl", "KXNFLPLAYOFF", None),
    ("nfl1", "AFC/NFC #1 seed", "nfl", "KXNFL1SEED", None),
    ("afce", "AFC East", "nfl", "KXNFLAFCEAST", None), ("afcn", "AFC North", "nfl", "KXNFLAFCNORTH", None),
    ("afcs", "AFC South", "nfl", "KXNFLAFCSOUTH", None), ("afcw", "AFC West", "nfl", "KXNFLAFCWEST", None),
    ("nfce", "NFC East", "nfl", "KXNFLNFCEAST", None), ("nfcn", "NFC North", "nfl", "KXNFLNFCNORTH", None),
    ("nfcs", "NFC South", "nfl", "KXNFLNFCSOUTH", None), ("nfcw", "NFC West", "nfl", "KXNFLNFCWEST", None),
    ("nba", "NBA champion", "nba", "KXNBA", "basketball_nba_championship_winner"),
    ("nbae", "NBA East", "nba", "KXNBAEAST", None), ("nbaw", "NBA West", "nba", "KXNBAWEST", None),
    ("nbapo", "Make the NBA playoffs", "nba", "KXNBAPLAYOFF", None),
    ("nbacup", "NBA Cup", "nba", "KXNBACUP", None),
    ("nbaatl", "Atlantic", "nba", "KXNBAATLANTIC", None), ("nbacen", "Central", "nba", "KXNBACENTRAL", None),
    ("nbase", "Southeast", "nba", "KXNBASOUTHEAST", None), ("nbanw", "Northwest", "nba", "KXNBANORTHWEST", None),
    ("nbapac", "Pacific", "nba", "KXNBAPACIFIC", None), ("nbasw", "Southwest", "nba", "KXNBASOUTHWEST", None),
    ("nhl", "Stanley Cup", "nhl", "KXNHL", "icehockey_nhl_championship_winner"),
    ("nhle", "NHL East", "nhl", "KXNHLEAST", None), ("nhlw", "NHL West", "nhl", "KXNHLWEST", None),
    ("nhlpo", "Make the NHL playoffs", "nhl", "KXNHLPLAYOFF", None),
    ("nhlpres", "Presidents' Trophy", "nhl", "KXNHLPRES", None),
    ("nhlatl", "Atlantic", "nhl", "KXNHLATLANTIC", None), ("nhlmet", "Metropolitan", "nhl", "KXNHLMETROPOLITAN", None),
    ("nhlcen", "Central", "nhl", "KXNHLCENTRAL", None), ("nhlpac", "Pacific", "nhl", "KXNHLPACIFIC", None),
    ("cfb", "CFB national champion", "ncaaf", "KXNCAAF", "americanfootball_ncaaf_championship_winner"),
    ("cfp", "Make the CFP", "ncaaf", "KXNCAAFPLAYOFF", None),
    ("cfpqf", "CFP quarterfinals", "ncaaf", "KXNCAAFQF", None),
    ("cfpsf", "CFP semifinals", "ncaaf", "KXNCAAFSF", None),
    ("cfptop", "CFP top seed", "ncaaf", "KXNCAAFTOPSEED", None),
    ("sec", "SEC champion", "ncaaf", "KXNCAAFSEC", None), ("b10", "Big Ten champion", "ncaaf", "KXNCAAFB10", None),
    ("b12", "Big 12 champion", "ncaaf", "KXNCAAFB12", None), ("acc", "ACC champion", "ncaaf", "KXNCAAFACC", None),
    ("aac", "AAC champion", "ncaaf", "KXNCAAFAAC", None), ("mwc", "Mountain West champion", "ncaaf", "KXNCAAFMWC", None),
    ("sbelt", "Sun Belt champion", "ncaaf", "KXNCAAFSBELT", None), ("mac", "MAC champion", "ncaaf", "KXNCAAFMAC", None),
    ("cusa", "C-USA champion", "ncaaf", "KXNCAAFCUSA", None),
    ("mm", "March Madness champion", "ncaab", "KXMARMAD", "basketball_ncaab_championship_winner"),
    ("mmround", "March Madness rounds", "ncaab", "KXMARMADROUND", None),
    ("masters", "Masters", "golf", None, "golf_masters_tournament_winner"),
    ("pgachamp", "PGA Championship", "golf", None, "golf_pga_championship_winner"),
    ("usopen", "US Open (golf)", "golf", None, "golf_us_open_winner"),
    ("open", "The Open", "golf", None, "golf_the_open_championship_winner"),
]
WEIGHT = {"pinnacle": 3.0, "betfair_ex_eu": 3.0, "lowvig": 1.5}
SHARP_BOOKS = {"pinnacle", "betfair_ex_eu"}
KALSHI_ANCHOR = {"cfb", "mm"}
PROP_KEYS = {"nflpo", "nfl1", "nbapo", "nhlpo", "cfp", "cfpqf", "cfpsf", "mmround"}   # many teams can hit: no 100% total           # no sharp book prices these on the Odds API: Kalshi anchors the fair line
BAR, BAR_LONG, LONG_P = 0.05, 0.08, 0.03
MIN_FAIR = 0.03            # no alerts on picks under a 3% fair chance -- longshot prices are where the noise lives
MAX_EV = 0.25              # an 'edge' above this on a future is a pricing mistake, not value
KALSHI_MIN_MID = 0.04      # below this Kalshi's price is mostly its 1c tick + lottery demand
KELLY_DIV = 16
BETTER_EV = 0.02
FUT_BOOKS = (S.BET_BOOKS - {"draftkings"}) | {"kalshi"}   # places you can bet


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def kalshi_events(series):
    """{event_ticker: {"title", "close", "sels": {name: dict(bid, ask, mid, spread, vol, ticker)}}} for open markets."""
    out, cursor = {}, None
    for _ in range(10):
        d = S._get(f"{KAL}/markets", dict(series_ticker=series, status="open", limit=1000, **({"cursor": cursor} if cursor else {})))
        for m in (d or {}).get("markets", []):
            name = (m.get("yes_sub_title") or m.get("subtitle") or "").strip()
            bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
            if not name or ask is None or ask <= 0 or ask >= 1:
                continue
            mid = (bid + ask) / 2 if bid and bid > 0 else None
            ev = out.setdefault(m["event_ticker"], dict(title=m.get("title") or "", sels={},
                                                         close=m.get("expected_expiration_time") or m.get("close_time")))
            ev["sels"][name] = dict(bid=bid or 0.0, ask=ask, mid=mid, spread=(ask - bid) if bid else None,
                                    vol=_f(m.get("volume_fp")) or 0.0, ticker=m.get("ticker"))
        cursor = (d or {}).get("cursor")
        if not cursor:
            break
        time.sleep(0.2)
    return out


def odds_outright(okey):
    """{book: {name: american odds}} from the Odds API (one call, every region)."""
    key = S.os.environ.get("ODDS_API_KEY")
    if not key:
        return {}, 0
    r = requests.get(f"{S.ODDS}/{okey}/odds", timeout=60, params=dict(
        apiKey=key, regions="us,us2,us_ex,eu", markets="outrights", oddsFormat="american"))
    S.note_used(r)
    if r.status_code in (404, 422) or not r.ok:
        return {}, 0
    books = {}
    for e in r.json():
        for b in e.get("bookmakers", []):
            for mk in b.get("markets", []):
                books.setdefault(b["key"], {}).update({o["name"]: o["price"] for o in mk.get("outcomes", [])})
    return books, int(r.headers.get("x-requests-last") or 0)


def match(name, names):
    """The one name in `names` that is the same team / player as `name`, or None."""
    n = S.norm(name)
    exact = [x for x in names if S.norm(x) == n]
    if exact:
        return exact[0]
    hits = [x for x in names if S.name_hits(name, x) or S.name_hits(x, name)]
    return hits[0] if len(hits) == 1 else None


def book_probs(prices):
    """De-vig one book's whole field (power method) -> {name: fair chance}."""
    names = list(prices)
    imps = [S.prob(prices[n]) for n in names]
    if len(names) < 3 or sum(imps) < 0.9:
        return {}
    return dict(zip(names, S.devig_n(imps)))


def price_market(key, sport, sels, books):
    """Fair chance per Kalshi/odds name, plus every bettable price. Returns (rows, anchored)."""
    names = list(sels) or sorted({n for b in books.values() for n in b})
    canon = {}   # book outcome name -> our name
    for b in books.values():
        for bn in b:
            if bn not in canon:
                canon[bn] = match(bn, names) if sels else bn
    acc = {n: [] for n in names}
    for bk, prices in books.items():
        for bn, p in book_probs(prices).items():
            n = canon.get(bn)
            if n in acc:
                acc[n].append((WEIGHT.get(bk, 1.0), p, bk))
    kal_mid = {n: s["mid"] for n, s in sels.items() if s.get("mid")}
    ksum = sum(kal_mid.values())
    winner = key not in PROP_KEYS
    for n, m in kal_mid.items():
        if winner and ksum > 0:
            m = m / ksum   # a winner field must add to 100%
        wk = 2.0 if (sels[n].get("spread") or 1) <= 0.06 else 1.0
        if books and sels[n]["mid"] < KALSHI_MIN_MID:
            continue   # Kalshi longshots trade at 1-2c no matter what (1c tick, lottery buyers): books price those
        acc.setdefault(n, []).append((wk if key not in KALSHI_ANCHOR else 3.0, m, "kalshi"))
    rows = []
    for n, xs in acc.items():
        if not xs:
            continue
        fair = sum(w * p for w, p, _ in xs) / sum(w for w, _, _ in xs)
        rows.append(dict(name=n, fair=fair, srcs={bk for _, _, bk in xs}))
    if winner:
        tot = sum(r["fair"] for r in rows) or 1
        for r in rows:
            r["fair"] /= tot
    anchored = any(bk in SHARP_BOOKS for b in [books] for bk in b) or key in KALSHI_ANCHOR
    for r in rows:   # bettable prices for this name
        offers = []
        for bk, prices in books.items():
            bn = next((x for x in prices if canon.get(x) == r["name"]), None)
            if bn is not None and bk in FUT_BOOKS:
                offers.append((bk, int(prices[bn]), None))
        s = sels.get(r["name"])
        if s:
            offers.append(("kalshi", S.american(s["ask"]), s["ask"]))
        r["offers"] = offers
    return rows, anchored and bool(books or key in KALSHI_ANCHOR)


def ev_of(fair, odds, book, sport):
    return fair * (1 + S.payout(odds, book, sport)) - 1


def best_offer(r, sport):
    best = None
    for bk, odds, pr in r["offers"]:
        e = ev_of(r["fair"], odds, bk, sport)
        if best is None or e > best[3]:
            best = (bk, odds, pr, e)
    return best


def kelly_stake(fair, odds, book, sport):
    b = S.payout(odds, book, sport)
    f = (fair * (b + 1) - 1) / b if b > 0 else 0
    return max(1, round(max(f, 0) / KELLY_DIV * S.BANKROLL))


# ---------------- Discord ----------------
_FCH = [None]


def futures_channel():
    if _FCH[0] is None:
        try:
            gid = S.bot("GET", f"/channels/{S.channel_id()}")["guild_id"]
            chans = S.bot("GET", f"/guilds/{gid}/channels") or []
            _FCH[0] = next((c["id"] for c in chans if "future" in (c.get("name") or "").lower() and c.get("type") == 0), "")
        except Exception as e:  # noqa: BLE001
            S.log(f"futures channel lookup failed: {type(e).__name__}")
            _FCH[0] = ""
    return _FCH[0]


def post(text, edit_id=None, buttons=False):
    if S.DRY or not S.BOT:
        print("   [futures]" + (" [edit]" if edit_id else ""), text.replace("\n", " | ")[:400])
        return edit_id or f"dry-{abs(hash(text)) % 10**8}"
    ch = futures_channel()
    if not ch:
        return None
    body = {"content": text[:1990], **({"components": S.BUTTONS} if buttons else {})}
    if edit_id:
        try:
            S.bot("PATCH", f"/channels/{ch}/messages/{edit_id}", json=body)
            return edit_id
        except Exception:  # noqa: BLE001
            pass
    m = S.bot("POST", f"/channels/{ch}/messages", json=body)
    if buttons:
        S.BOT_MSGS.add(m["id"])
    return m["id"]


def pin(mid):
    if S.DRY or not S.BOT or not mid:
        return
    try:
        S.bot("PUT", f"/channels/{futures_channel()}/pins/{mid}")
    except Exception:  # noqa: BLE001 -- needs Manage Messages; the board still works unpinned
        pass


def pct(p):
    return f"{p * 100:.1f}%" if p >= 0.01 else f"{p * 100:.2f}%"


def odds_str(o):
    return f"{o:+d}"


# ---------------- the daily run ----------------
def run(force=False):
    now = datetime.now(timezone.utc)
    today = now.astimezone(S.ET).strftime("%Y-%m-%d")
    st = S.fs_get("scan_state/futures") or {}
    if st.get("day") == today and not force:
        return S.log("futures: already ran today")
    credits = 0
    board = {}       # sport -> list of (market label, rows)
    alerts = 0
    seen = {p["_id"]: p for p in S.fs_recent("scan_plays", "start", now.isoformat()) if p.get("kind") == "future"}
    for key, label, sport, series, okey in MARKETS:
        try:
            evs = kalshi_events(series) if series else {}
            books, c = odds_outright(okey) if okey else ({}, 0)
            credits += c
        except Exception as e:  # noqa: BLE001 -- one market down never stops the board
            S.log(f"futures [{key}]: {type(e).__name__}: {str(e)[:80]}")
            continue
        groups = list(evs.items()) or ([("", dict(title=label, sels={}, close=None))] if books else [])
        for et, ev in groups:
            sub = label + (f" · {round_label(et)}" if len(groups) > 1 else "")
            rows, anchored = price_market(key, sport, ev["sels"], books if len(groups) == 1 else {})
            if not rows:
                continue
            rows.sort(key=lambda r: -r["fair"])
            snap_id = f"{key}_{re.sub(r'[^A-Za-z0-9]', '', et) or 'x'}"
            prev = S.fs_get(f"scan_futures/{snap_id}_{(now - timedelta(days=7)).astimezone(S.ET).strftime('%Y-%m-%d')}") or {}
            S.fs_set(f"scan_futures/{snap_id}_{today}", dict(day=today, market=key, label=sub, sport=sport,
                                                             fair={r["name"]: round(r["fair"], 5) for r in rows}))
            wk = prev.get("fair") or {}
            for r in rows:
                r["move"] = (r["fair"] - wk[r["name"]]) if r["name"] in wk else None
                r["best"] = best_offer(r, sport if sport != "golf" else "nfl")
            board.setdefault(sport, []).append((sub, rows))
            if anchored:
                alerts += alert_market(key, sub, sport, et, ev, rows, seen, now)
    dashboards(board, st)
    S.fs_set("scan_state/futures", dict(st, day=today, at=now.isoformat()))
    S.log(f"futures: {sum(len(v) for v in board.values())} markets, {alerts} alert(s)/update(s), {credits} credits")


def round_label(event_ticker):
    tag = (event_ticker or "").split("-")[-1]
    tag = re.sub(r"^\d+", "", tag)   # '27R16' -> 'R16'
    names = {"R76": "make the tournament", "R68": "make the tournament", "R64": "make round of 64",
             "R32": "make round of 32", "R16": "Sweet 16", "R8": "Elite 8", "F4": "Final Four", "T2": "title game"}
    return names.get(tag, tag or event_ticker)


def alert_market(key, sub, sport, et, ev, rows, seen, now):
    n = 0
    live = set()
    fee_sport = sport if sport != "golf" else "nfl"
    for r in rows:
        b = r["best"]
        if not b:
            continue
        bk, odds, pr, e = b
        bar = BAR_LONG if r["fair"] < LONG_P else BAR
        if r["fair"] < MIN_FAIR or e < bar or e > MAX_EV or (len(r["srcs"]) < 2 and key not in KALSHI_ANCHOR):
            continue
        if bk == "kalshi" and key in KALSHI_ANCHOR:
            continue   # Kalshi is the fair line here: can't be value against itself
        pid = re.sub(r"[^A-Za-z0-9_.+-]", "_", f"fut_{key}_{et}_{r['name']}")
        live.add(pid)
        old = seen.get(pid)
        if old and e < (old.get("ev") or 0) + BETTER_EV:
            continue
        stake = kelly_stake(r["fair"], odds, bk, fee_sport)
        price = S.price_str(odds, bk, pr)
        bkn = S.BOOK_NAMES.get(bk, bk)
        head = (f"🔄 **Better number** -- first posted at {old['first_price']} @ {old['first_book']}\n" if old else "")
        text = (f"{head}🏆 **{SPORT_LABEL.get(sport, sport)} future** · {sub}\n"
                f"**{r['name']} · {price} @ {bkn}**\n"
                f"Fair {pct(r['fair'])} ({odds_str(S.american(r['fair']))}) · EV {e:+.1%} · bet ${stake} (1/16 Kelly) · "
                f"money locked until {(ev.get('close') or '')[:10] or 'the season ends'}")
        mid = post(text, edit_id=(old or {}).get("msg_id"), buttons=True)
        close = ev.get("close") or (now + timedelta(days=180)).isoformat()
        doc = dict(old or {}, kind="future", sport=sport, fut=key, event_id=et, label=f"{r['name']} -- {sub}", side="a",
                   line=None, book=bk, odds=odds, odds_alerted=odds, price_prob=pr, ev=round(e, 4),
                   p_fair=round(r["fair"], 5), fair_odds=S.american(r["fair"]), stake_usd=stake, bar=bar,
                   start=close.replace("Z", "+00:00"), home=r["name"], away="", alerted=True, msg_id=mid,
                   msg_text=text, msg_base=text, via="bot", alerted_at=now.isoformat(),
                   first_price=(old or {}).get("first_price") or price, first_book=(old or {}).get("first_book") or bkn)
        S.fs_set(f"scan_plays/{pid}", doc)
        seen[pid] = dict(doc, _id=pid)
        n += 1
    # earlier alerts in this market that no longer clear the bar: say so on the message (bets you logged stay)
    fair = {r["name"]: r for r in rows}
    for pid, x in list(seen.items()):
        if x.get("fut") != key or x.get("event_id") != et or pid in live or x.get("withdrawn"):
            continue
        r = fair.get(x.get("home"))
        why = (f"fair now {pct(r['fair'])}, best EV {r['best'][3]:+.1%}" if r and r.get("best") else "no longer priced")
        if r and r["fair"] < MIN_FAIR:
            why = f"longshot (fair {pct(r['fair'])}) -- pricing noise, not value"
        txt = (x.get("msg_base") or x.get("msg_text") or "") + f"\n🚫 **No longer a play** -- {why}"
        post(txt, edit_id=x.get("msg_id"))
        x.update(withdrawn=now.isoformat(), msg_text=txt)
        S.fs_set(f"scan_plays/{pid}", {k: v for k, v in x.items() if k != "_id"})
        n += 1
    return n


def dashboards(board, st):
    """One pinned message per sport (edited daily): top of each market, fair chance, 7-day move, best price."""
    ids = dict(st.get("dash") or {})
    for sport, mkts in board.items():
        lines = [f"**{SPORT_LABEL.get(sport, sport)} futures board** · {datetime.now(S.ET).strftime('%a %b %-d')}",
                 "-# fair chance · 7-day move · best price available"]
        for sub, rows in mkts:
            key = next((k for k, lab, *_ in MARKETS if sub.startswith(lab)), "")
            if key in PROP_KEYS:   # make-the-playoffs style: the contested teams, not the locks
                show = [r for r in rows if 0.10 <= r["fair"] <= 0.90][:14] or rows[:8]
            else:
                show = rows[:8]
            lines.append("")
            lines.append(f"__**{sub}**__")
            for r in show:
                mv = ""
                if r.get("move") is not None and abs(r["move"]) >= 0.005:
                    mv = f" ({'▲' if r['move'] > 0 else '▼'}{abs(r['move']) * 100:.1f})"
                b = r.get("best")
                bp = f" · {odds_str(b[1])} {S.BOOK_NAMES.get(b[0], b[0])}" if b else ""
                lines.append(f"• {r['name']} — **{pct(r['fair'])}**{mv}{bp}")
        text, chunks = "", []
        for ln in lines:
            if len(text) + len(ln) + 1 > 1900:
                chunks.append(text)
                text = ""
            text += ln + "\n"
        chunks.append(text)
        old = ids.get(sport) or []
        new = []
        for i, ch in enumerate(chunks):
            mid = post(ch, edit_id=old[i] if i < len(old) else None)
            if mid and (i >= len(old) or mid != old[i]):
                pin(mid)
            new.append(mid)
        for extra in old[len(chunks):]:   # board got shorter: blank the leftover message
            post("​", edit_id=extra)
        ids[sport] = new
    st["dash"] = ids
