"""First-half spreads and totals for chosen NFL games: every book's 1H price vs Pinnacle's own 1H line (devigged) and
the other books' consensus at the same number. A price must beat BOTH by MIN_EV. Prints only.
python3 alts/h1_check.py "MIN@GB,SF@DAL,PIT@CIN" [min_ev]    About 4 credits per game."""
import os, sys
from datetime import datetime, timedelta, timezone
import requests
KEY = os.environ["ODDS_API_KEY"]; B = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
WANT = [g.strip().upper() for g in sys.argv[1].split(",") if g.strip()]
MIN_EV = float(sys.argv[2]) if len(sys.argv) > 2 else 0.02
SKIP = {b.strip() for b in os.environ.get("SKIP_BOOKS", "fanatics,bovada").split(",") if b.strip()}
BOOKS = ["pinnacle", "betonlineag", "espnbet", "williamhill_us", "betrivers", "betmgm", "fanduel", "draftkings",
         "ballybet", "kalshi", "polymarket", "novig", "prophetx"]
FEES = {"kalshi": ("parabolic", 0.07), "polymarket": ("parabolic", 0.07), "prophetx": ("winnings", 0.02)}
AB = {"Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF", "Carolina Panthers": "CAR",
      "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL", "Denver Broncos": "DEN",
      "Detroit Lions": "DET", "Green Bay Packers": "GB", "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
      "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC", "Los Angeles Rams": "LA", "Miami Dolphins": "MIA",
      "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG", "New York Jets": "NYJ",
      "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA",
      "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN", "Washington Commanders": "WAS"}
prob = lambda a: 100 / (a + 100) if a > 0 else -a / (-a + 100)
def payout(a, book):
    pay = a / 100 if a > 0 else 100 / -a
    kind, rate = FEES.get(book, (None, 0))
    if kind == "parabolic":
        p = 1 / (1 + pay); return 1 / (p + rate * p * (1 - p)) - 1
    return pay * (1 - rate) if kind else pay
now = datetime.now(timezone.utc)
evs = [e for e in requests.get(f"{B}/events", params={"apiKey": KEY}, timeout=30).json()
       if f"{AB.get(e['away_team'])}@{AB.get(e['home_team'])}" in WANT
       and datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) > now]
print(f"{len(evs)} of {len(WANT)} games found")
for e in evs:
    r = requests.get(f"{B}/events/{e['id']}/odds", params={"apiKey": KEY, "bookmakers": ",".join(BOOKS),
                     "markets": "spreads_h1,totals_h1", "oddsFormat": "american"}, timeout=30)
    home, away = e["home_team"], e["away_team"]
    two = {}   # (market, number) -> {book: (priceA, priceB)}  A = home / over
    for b in r.json().get("bookmakers", []):
        for m in b["markets"]:
            o = {x["name"]: x for x in m["outcomes"]}
            if m["key"] == "spreads_h1" and home in o and away in o and o[home].get("point") is not None:
                two.setdefault(("1H spread", o[home]["point"]), {})[b["key"]] = (o[home]["price"], o[away]["price"])
            if m["key"] == "totals_h1" and "Over" in o and "Under" in o:
                two.setdefault(("1H total", o["Over"]["point"]), {})[b["key"]] = (o["Over"]["price"], o["Under"]["price"])
    print(f"\n{away} @ {home} (credits left {r.headers.get('x-requests-remaining')})")
    for (mk, num), bks in sorted(two.items()):
        fair = {bk: prob(a) / (prob(a) + prob(b)) for bk, (a, b) in bks.items()}
        line = f"  {mk} {num:+g}" if mk == "1H spread" else f"  {mk} {num:g}"
        pin = fair.get("pinnacle")
        print(line + f": {len(bks)} books" + (f", Pinnacle fair {'home' if mk == '1H spread' else 'over'} {pin:.1%}" if pin else ", no Pinnacle"))
        for bk, (pa, pb) in bks.items():
            if bk in SKIP or bk == "pinnacle":
                continue
            others = [q for b2, q in fair.items() if b2 != bk]
            if pin is None or len(others) < 2:
                continue
            cons = sum(others) / len(others)
            for side, price, pp, pc in (("A", pa, pin, cons), ("B", pb, 1 - pin, 1 - cons)):
                ev = min(pp, pc) * payout(price, bk) - (1 - min(pp, pc))
                if ev >= MIN_EV:
                    name = (home if side == "A" else away).split()[-1] + f" {num if side == 'A' else -num:+g}" if mk == "1H spread" \
                        else ("Over" if side == "A" else "Under") + f" {num:g}"
                    print(f"     VALUE {mk}: {name} {price:+d} @ {bk}  edge {ev:+.1%} (vs Pinnacle {pp:.1%}, books {pc:.1%})")
