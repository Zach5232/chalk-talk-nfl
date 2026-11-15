"""
NFL alt spreads and alt totals vs the sharp fair line (prints only; nothing posted or saved).

For every game in the next HOURS hours: Pinnacle's main spread and total, devigged, are the fair anchor. Any other
number is priced by moving that anchor along real NFL results (key_numbers.json: how often games land exactly on
3, 7, 10, ...), the same key-number pricer the weekly model uses. Every book's alternate line (and main line) is
checked against it; EV is after exchange fees, pushes return the stake.

python3 alts/alt_check.py [min_ev]     ODDS_API_KEY in the environment. About 8 credits per game.
"""
import json, os, sys
from datetime import datetime, timedelta, timezone
import requests

KEY = os.environ["ODDS_API_KEY"]
B = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
MIN_EV = float(sys.argv[1]) if len(sys.argv) > 1 else 0.03
HOURS = 14
MAX_MOVE = 7.0           # only price numbers within this many points of Pinnacle's line
SKIP = {b.strip() for b in os.environ.get("SKIP_BOOKS", "fanatics,bovada").split(",") if b.strip()}
BOOKS = ["pinnacle", "betonlineag", "espnbet", "williamhill_us", "betrivers", "betmgm", "fanduel", "draftkings",
         "ballybet", "kalshi", "polymarket", "novig", "prophetx"]
FEES = {"kalshi": ("parabolic", 0.07), "polymarket": ("parabolic", 0.07), "prophetx": ("winnings", 0.02)}
PMF = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "key_numbers.json")))["pmf"]


def prob(a):
    a = float(a); return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def payout(a, book):
    a = float(a); pay = a / 100 if a > 0 else 100 / -a
    kind, rate = FEES.get(book, (None, 0))
    if kind == "parabolic":
        p = 1 / (1 + pay); return 1 / (p + rate * p * (1 - p)) - 1
    return pay * (1 - rate) if kind else pay


def pmf_at(kind, center):
    keys = sorted(PMF[kind], key=lambda c: abs(float(c) - center))
    return {int(k): v for k, v in PMF[kind][keys[0]].items()}


def pricer(kind, line, p_over):
    """Pinnacle at `line` with devigged P(over) -> fn(L) = (P(over L), P(push L)). For spreads, 'over' is the HOME
    margin beating -line (home covering); the distribution is centered on the expected home margin."""
    center = line if kind == "total" else -line
    pmf = pmf_at(kind, center)
    tot = sum(pmf.values())
    pmf = {k: v / tot for k, v in pmf.items()}
    thr = line if kind == "total" else -line                      # outcome must exceed thr
    over_c = p_over * (1 - pmf.get(thr, 0.0)) if float(thr).is_integer() else p_over

    def f(L):
        t = L if kind == "total" else -L
        if t >= thr:
            po = over_c - sum(v for k, v in pmf.items() if thr < k <= t)
        else:
            po = over_c + sum(v for k, v in pmf.items() if t < k <= thr)
        push = pmf.get(int(t), 0.0) if float(t).is_integer() else 0.0
        return min(max(po, 0.0), 1.0), push
    return f


now = datetime.now(timezone.utc)
evs = requests.get(f"{B}/events", params={"apiKey": KEY}, timeout=30).json()
evs = [e for e in evs if now < datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) < now + timedelta(hours=HOURS)]
print(f"{len(evs)} games in the next {HOURS}h")
found = []
for e in evs:
    r = requests.get(f"{B}/events/{e['id']}/odds", params={"apiKey": KEY, "bookmakers": ",".join(BOOKS),
                     "markets": "spreads,totals,alternate_spreads,alternate_totals", "oddsFormat": "american"}, timeout=30)
    d = r.json(); home, away = e["home_team"], e["away_team"]
    left = r.headers.get("x-requests-remaining")
    bms = {b["key"]: {m["key"]: m["outcomes"] for m in b["markets"]} for b in d.get("bookmakers", [])}
    pin = bms.get("pinnacle", {})
    fair = {}
    sp = {o["name"]: o for o in pin.get("spreads", [])}
    if home in sp and away in sp:
        h = sp[home]; q = prob(h["price"]) / (prob(h["price"]) + prob(sp[away]["price"]))
        fair["spread"] = (h["point"], pricer("spread", h["point"], q))
    tt = {o["name"]: o for o in pin.get("totals", [])}
    if "Over" in tt and "Under" in tt:
        q = prob(tt["Over"]["price"]) / (prob(tt["Over"]["price"]) + prob(tt["Under"]["price"]))
        fair["total"] = (tt["Over"]["point"], pricer("total", tt["Over"]["point"], q))
    if not fair:
        print(f"  {away} @ {home}: no Pinnacle line, skipped"); continue
    for bk, mk in bms.items():
        if bk == "pinnacle" or bk in SKIP:
            continue
        for mkey in ("spreads", "alternate_spreads"):
            if "spread" not in fair:
                continue
            main, f = fair["spread"]
            for o in mk.get(mkey, []):
                if o.get("point") is None or abs(float(o["price"])) > 400:
                    continue
                L = o["point"] if o["name"] == home else -o["point"]    # as a home handicap
                if abs(L - main) > MAX_MOVE:
                    continue
                po, push = f(L)
                pw = po if o["name"] == home else 1 - po - push
                ev = pw * payout(o["price"], bk) - (1 - pw - push)
                found.append((ev, f"{away} @ {home}", f"{o['name'].split()[-1]} {o['point']:+g}", o["price"], bk,
                              "alt" if mkey.startswith("alt") else "main", pw, push, f"Pinnacle {home.split()[-1]} {main:+g}"))
        for mkey in ("totals", "alternate_totals"):
            if "total" not in fair:
                continue
            main, f = fair["total"]
            for o in mk.get(mkey, []):
                if o.get("point") is None or abs(float(o["price"])) > 400 or abs(o["point"] - main) > MAX_MOVE:
                    continue
                po, push = f(o["point"])
                pw = po if o["name"] == "Over" else 1 - po - push
                ev = pw * payout(o["price"], bk) - (1 - pw - push)
                found.append((ev, f"{away} @ {home}", f"{o['name']} {o['point']:g}", o["price"], bk,
                              "alt" if mkey.startswith("alt") else "main", pw, push, f"Pinnacle total {main:g}"))
    print(f"  {away} @ {home}: priced ({len(bms)} books), credits left {left}")

best = {}
for x in sorted(found, key=lambda x: -x[0]):
    k = (x[1], x[2])
    if x[0] >= MIN_EV and k not in best:
        best[k] = x
print(f"\nAlt and main lines {MIN_EV:.0%}+ better than Pinnacle's fair line (best book per line):")
for ev, game, bet, price, bk, kind, pw, push, ref in sorted(best.values(), key=lambda x: -x[0])[:40]:
    print(f"  {ev:+6.1%}  {game:44} {bet:22} {price:+5d} @ {bk:12} {kind:4} win {pw:.1%}" + (f" push {push:.1%}" if push else "") + f"  [{ref}]")
