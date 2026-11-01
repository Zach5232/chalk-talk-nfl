"""Anytime TDs where a sportsbook pays more than Kalshi's fair price (bid/ask midpoint), plus 2-3 leg parlays of them
from different games. The one TD rule that held up in the backtest (book beats Kalshi mid 5%+: +12% on 66 bets).
python3 tdcard/kalshi_edge.py <odds.json> [min_edge]   -- prints only; nothing is posted or saved."""
import itertools, json, re, sys, urllib.parse, urllib.request

ODDS, MIN = sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 0.05
dec = lambda a: 1 + a / 100 if a > 0 else 1 + 100 / -a
amer = lambda d: f"{(d - 1) * 100:+.0f}" if d >= 2 else f"{-100 / (d - 1):.0f}"


def norm(n):
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", str(n).lower())
    return re.sub(r"[^a-z]", "", n)


mid, cursor = {}, None
for _ in range(20):
    q = dict(series_ticker="KXNFLTD", status="open", limit=1000, **({"cursor": cursor} if cursor else {}))
    req = urllib.request.Request("https://api.elections.kalshi.com/trade-api/v2/markets?" + urllib.parse.urlencode(q),
                                 headers={"User-Agent": "td-card"})
    d = json.load(urllib.request.urlopen(req, timeout=20))
    for m in d.get("markets", []):
        bid, ask = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
        if m["title"].endswith(": 1+ touchdowns") and 0 < bid < ask < 1 and ask - bid <= .03:
            mid[norm(m["title"][:-15])] = (bid + ask) / 2
    cursor = d.get("cursor")
    if not cursor or not d.get("markets"):
        break
print(f"kalshi: {len(mid)} players with a tight market")

legs = []
for gm in json.load(open(ODDS)):
    best = {}
    for bk in gm.get("bookmakers", []):
        for mk in bk.get("markets", []):
            for o in mk.get("outcomes", []):
                if o.get("name") == "Yes" and o.get("description"):
                    k = norm(o["description"])
                    if k not in best or dec(o["price"]) > dec(best[k][0]):
                        best[k] = (o["price"], bk["title"], o["description"])
    for k, (price, book, name) in best.items():
        if k in mid:
            edge = dec(price) * mid[k] - 1
            legs.append(dict(game=gm["game_id"], kick=gm["kickoff"], name=name, price=price, book=book, fair=mid[k], edge=edge))
legs.sort(key=lambda x: -x["edge"])
print(f"\nSingles: book beats Kalshi fair by {MIN:.0%}+ ({sum(l['edge'] >= MIN for l in legs)} of {len(legs)} matched)")
for l in legs:
    if l["edge"] >= MIN:
        print(f"  {l['name']:24} {l['game'][8:]:8} {l['kick'][11:16]}Z  {l['price']:+d} @ {l['book']:12} "
              f"Kalshi fair {amer(1 / l['fair'])}  edge {l['edge']:+.0%}")
good = [l for l in legs if l["edge"] >= MIN]
for n in (2, 3):
    combos = [c for c in itertools.combinations(good, n) if len({l["game"] for l in c}) == n]
    scored = []
    for c in combos:
        p = 1
        pay = 1
        for l in c:
            p *= l["fair"]; pay *= dec(l["price"])
        scored.append((p * pay - 1, p, pay, c))
    scored.sort(key=lambda s: -s[0])
    print(f"\n{n}-leg parlays (different games; fair = Kalshi, payout if each leg is bet at its best book and parlayed at true odds):")
    for ev, p, pay, c in scored[:6]:
        print(f"  {' + '.join(l['name'] for l in c)}  pays {amer(pay)}  hits {p:.1%}  EV {ev:+.0%}")
