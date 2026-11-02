"""One-off: what college basketball markets do Kalshi, Novig and Polymarket list, and how are they named?"""
import json, urllib.parse, urllib.request
UA = {"User-Agent": "cbb-probe"}
def get(url, **q):
    req = urllib.request.Request(url + ("?" + urllib.parse.urlencode(q) if q else ""), headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=30))
K = "https://api.elections.kalshi.com/trade-api/v2"
try:
    ser = get(f"{K}/series", category="Sports").get("series", [])
    cbb = [s for s in ser if "NCAAMB" in s["ticker"] or "college basketball" in s.get("title", "").lower()]
    for s in cbb: print("KALSHI series", s["ticker"], "|", s["title"])
    for s in cbb:
        ms = get(f"{K}/markets", series_ticker=s["ticker"], status="open", limit=1000).get("markets", [])
        print(f"-- {s['ticker']}: {len(ms)} open")
        for m in ms[:4]:
            print("   ", {k: m.get(k) for k in ("ticker", "event_ticker", "title", "yes_sub_title", "subtitle", "yes_bid_dollars", "yes_ask_dollars", "yes_ask_size_fp", "occurrence_datetime", "expected_expiration_time", "floor_strike", "strike_type")})
except Exception as e: print("kalshi error", e)
N = "https://api.novig.com/v3/public"
for lg in ("NCAAB", "NCAAMB", "CBB", "NCAAM"):
    try:
        ev = get(f"{N}/catalog/events", league=lg).get("items", [])
        mk = get(f"{N}/catalog/markets", league=lg).get("items", [])
        print(f"NOVIG {lg}: {len(ev)} events, {len(mk)} markets")
        if ev: print("   event", {k: ev[0].get(k) for k in ("eventId", "description", "startsTs", "league", "status")})
        for m in mk[:3]: print("   market", {k: m.get(k) for k in ("eventId", "type", "marketType", "strike", "status", "startsTs")}, [(o.get("name"), o.get("bid"), o.get("ask")) for o in m.get("outcomes", [])])
        if mk:
            from collections import Counter; print("   types", Counter(m.get("type") or m.get("marketType") for m in mk).most_common())
    except Exception as e: print("novig", lg, "error", e)
G = "https://gamma-api.polymarket.com"
try:
    sp = get(f"{G}/sports")
    for s in sp:
        if any(x in (s.get("sport") or "").lower() for x in ("cbb", "ncaab", "cbb", "ncaam", "mbb")): print("POLY sport", s)
    s = next((s for s in sp if (s.get("sport") or "").lower() in ("cbb", "ncaab", "ncaam")), None)
    if s:
        ev = get(f"{G}/events", series_id=s["series"], closed="false", limit=100)
        print(f"POLY {s['sport']}: {len(ev)} open events")
        for e in ev[:3]:
            print("   ", e.get("title"), e.get("startDate"), [(m.get("sportsMarketType"), m.get("outcomes"), m.get("line"), m.get("gameStartTime")) for m in e.get("markets", [])[:3]])
except Exception as e: print("poly error", e)
