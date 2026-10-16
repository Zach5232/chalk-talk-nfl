"""
Smart book selection + Discord play alerts (shared design with the CFB and NHL models).

Books: the Odds API bills per group of up to 10 bookmakers, so all 13 books cost 2x what 10 do.
Every event gets at least one full 13-book scan; after that a pull asks for Pinnacle + the 9 bet books
that have most often been at (or within ~1% of) the best price on recent full scans. Which events are
on the board comes from the free /events endpoint, so deciding costs nothing.

State (a plain dict the caller loads/saves, e.g. a Firestore doc):
  full_events  event ids that have had a full scan
  pulls        recent full scans: {hits: {book: n}, opps: {book: n}}
  sent         Discord plays already posted: {key: decimal odds posted}
"""
import json
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

BOOK_NAMES = {"betonlineag": "BetOnline", "espnbet": "ESPN BET", "williamhill_us": "Caesars", "betrivers": "BetRivers",
              "betmgm": "BetMGM", "fanduel": "FanDuel", "draftkings": "DraftKings", "ballybet": "Bally Bet",
              "kalshi": "Kalshi", "polymarket": "Polymarket", "novig": "Novig", "prophetx": "ProphetX",
              "pinnacle": "Pinnacle"}
NEAR_BEST = 0.01
N_BET_BOOKS = 9
# tie-break before any scores exist: the exchanges first (where most bets go), then the books
BOOK_PRIORITY = ["novig", "prophetx", "kalshi", "polymarket", "betonlineag", "ballybet", "betrivers",
                 "williamhill_us", "betmgm", "espnbet", "fanduel", "draftkings"]


def upcoming_event_ids(sport_key, api_key, days=8):
    """Event ids starting within `days` (free endpoint, no credits)."""
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/?apiKey={api_key}"
    with urllib.request.urlopen(url, timeout=20) as r:
        events = json.load(r)
    now = datetime.now(timezone.utc)
    out = []
    for e in events:
        t = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        if now < t <= now + timedelta(days=days):
            out.append(e["id"])
    return out


def ranking(state, bet_books):
    hits, opps = defaultdict(int), defaultdict(int)
    for p in state.get("pulls", []):
        for bk, n in p.get("hits", {}).items():
            hits[bk] += n
        for bk, n in p.get("opps", {}).items():
            opps[bk] += n
    prio = {bk: i for i, bk in enumerate(BOOK_PRIORITY)}
    return sorted(bet_books, key=lambda bk: (-(hits[bk] + 1) / (opps[bk] + 2), prio.get(bk, 99)))


def choose(state, upcoming_ids, sharp, bet_books, force_full=False):
    """('full'|'reduced', books, reason)."""
    scanned = set(state.get("full_events", []))
    missing = [e for e in upcoming_ids if e not in scanned]
    if force_full:
        return "full", [sharp] + bet_books, "full scan requested"
    if missing or not scanned:
        return "full", [sharp] + bet_books, f"{len(missing)} event(s) without a full scan yet"
    return "reduced", [sharp] + ranking(state, bet_books)[:N_BET_BOOKS], "every event already had a full scan"


def _dec(american):
    a = float(american)
    return 1 + a / 100 if a > 0 else 1 + 100 / -a


def record_full_scan(state, raw_events, bet_books):
    """After a full pull: mark its events scanned and score each bet book against the best price."""
    hits, opps = defaultdict(int), defaultdict(int)
    for ev in raw_events or []:
        offers = defaultdict(dict)  # (market, outcome, point) -> {book: decimal}
        for bk in ev.get("bookmakers", []):
            if bk.get("key") not in bet_books:
                continue
            for mk in bk.get("markets", []):
                for o in mk.get("outcomes", []):
                    offers[(mk["key"], o.get("name"), o.get("description"), o.get("point"))][bk["key"]] = _dec(o["price"])
        for prices in offers.values():
            if len(prices) < 2:
                continue
            best = max(prices.values())
            for bk, d in prices.items():
                opps[bk] += 1
                hits[bk] += d >= best * (1 - NEAR_BEST)
    state["full_events"] = sorted(set(state.get("full_events", [])) | {ev["id"] for ev in raw_events or [] if ev.get("id")})[-400:]
    state["pulls"] = (state.get("pulls", []) + [{"hits": dict(hits), "opps": dict(opps)}])[-40:]
    return state


def post_plays(webhook_url, plays, state):
    """plays: [{key, dec, text}]. Posts each new play, or a play whose price improved; updates state['sent']."""
    if not webhook_url:
        return state
    sent = state.setdefault("sent", {})
    for p in plays:
        prev = sent.get(p["key"])
        if prev is not None and p["dec"] <= prev + 1e-9:
            continue
        body = json.dumps({"content": p["text"] + ("  _(price improved)_" if prev is not None else "")}).encode()
        req = urllib.request.Request(webhook_url, data=body, headers={"Content-Type": "application/json",
                                                                       "User-Agent": "model-alerts"})
        try:
            urllib.request.urlopen(req, timeout=15)
            sent[p["key"]] = p["dec"]
        except Exception as e:  # noqa: BLE001 -- an alert failure must never break the model run
            print(f"[discord] {e}")
        time.sleep(0.6)  # webhooks allow ~5 messages per 2 seconds
    return state
