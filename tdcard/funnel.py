"""
Weekly anytime-TD card: filter the priced player list down to 5-20 picks in visible steps, then write the Discord
message and the picks file used for Monday's grading.

python3 tdcard/funnel.py <season> <week>     (reads tdcard/card_<season>_wk<week>.csv from card.py)
Writes tdcard/discord_card.txt and tdcard/picks/<season>_wk<week>.csv. Fun money: no proven edge.
"""
import json, math, os, re, sys, urllib.parse, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SEASON, WEEK = int(sys.argv[1]), int(sys.argv[2])
MAX_PICKS, MIN_PICKS = 20, 5
STAKE = 5  # dollars per pick, shown on the card
BOOKS = {"draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM", "williamhill_us": "Caesars",
         "espnbet": "ESPN BET", "betrivers": "BetRivers", "fanatics": "Fanatics", "ballybet": "Bally Bet",
         "betonlineag": "BetOnline", "hardrockbet": "Hard Rock", "bovada": "Bovada", "kalshi": "Kalshi (fee incl.)"}
dec = lambda a: 1 + a / 100 if a > 0 else 1 + 100 / -a
amer = lambda p: f"{(1/p-1)*100:+.0f}" if p < .5 else f"{-100/(1/p-1):.0f}"
C = pd.read_csv(f"{HERE}/card_{SEASON}_wk{WEEK}.csv")


def wind(gid):
    """Forecast wind (mph) from the NFL pipeline's public weather docs; None if missing."""
    _, _, away, home = gid.split("_")
    url = ("https://firestore.googleapis.com/v1/projects/stock-model-42fb2/databases/(default)/documents/weather/"
           f"{away.lower()}-{home.lower()}")
    try:
        f = json.load(urllib.request.urlopen(url, timeout=10)).get("fields", {})
        for k in ("wind_mph", "wind", "windspeed", "wind_speed"):
            if k in f:
                v = f[k]; return float(v.get("doubleValue", v.get("integerValue", 0)))
    except Exception:  # noqa: BLE001 -- weather is a tiebreak, never a blocker
        return None
    return None


def norm(n):
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", str(n).lower())
    return re.sub(r"[^a-z]", "", n)


def kalshi_td():
    """Kalshi anytime-TD buy prices ('<Player>: 1+ touchdowns'), fee included: {norm name: (prob, $ available)}.
    Kalshi's taker fee is 7% x p x (1-p) per contract, rounded up to the cent. Empty if Kalshi can't be reached."""
    out, cursor = {}, None
    try:
        for _ in range(20):
            q = dict(series_ticker="KXNFLTD", status="open", limit=1000, **({"cursor": cursor} if cursor else {}))
            req = urllib.request.Request("https://api.elections.kalshi.com/trade-api/v2/markets?" + urllib.parse.urlencode(q),
                                         headers={"User-Agent": "td-card"})
            d = json.load(urllib.request.urlopen(req, timeout=20))
            for m in d.get("markets", []):
                ask = float(m.get("yes_ask_dollars") or 0)
                if m["title"].endswith(": 1+ touchdowns") and 0 < ask < 1:
                    eff = ask + math.ceil(round(7 * ask * (1 - ask), 6)) / 100
                    out[norm(m["title"][:-14])] = (eff, ask * float(m.get("yes_ask_size_fp") or 0))
            cursor = d.get("cursor")
            if not cursor or not d.get("markets"):
                break
    except Exception as e:  # noqa: BLE001 -- Kalshi is an extra price, never a blocker
        print(f"kalshi: {e}")
    return out


# Kalshi as one more "book": bet wherever pays more (needs $20+ available at the ask to count)
K = kalshi_td()
print(f"kalshi: {len(K)} anytime-TD prices")
kp = C.name.map(lambda n: K.get(norm(n), (None, 0)))
C["k_p"], C["k_avail"] = [x[0] for x in kp], [x[1] for x in kp]
use_k = C.k_p.notna() & (C.k_avail >= 20) & (1 / C.k_p > C.best.map(dec))
C.loc[use_k, "best"] = [round((1 / x - 1) * 100) if x < .5 else round(-100 / (1 / x - 1)) for x in C.k_p[use_k]]
C.loc[use_k, "bestbook"] = "kalshi"
C["value"] = C.p * C.best.map(dec) - 1

steps = []
S = C.copy(); steps.append(("Every priced player", len(S)))
S = S[S.pos != "QB"]; steps.append(("Not a QB (the model overrates QB rushing TDs: backtest 6.5% hit vs 9.6% priced)", len(S)))
S = S[S.value >= .10]; steps.append(("Model value 10%+ (any price)", len(S)))
S = S.assign(pb=0.5 * S.p + 0.5 * S.mkt * 0.87)
S = S.assign(vb=S.pb * S.best.map(dec) - 1)
S = S[S.vb >= .08]; steps.append(("Still 8%+ after blending with the books", len(S)))
S = S[~S.status.isin(["Questionable", "Out", "Doubtful"]) & ~S.boosted]; steps.append(("Healthy, not an injury fill-in", len(S)))
S = S[(S.snap >= .55) & ~(S.snap_last < S.snap - .15)]; steps.append(("Snap share 55%+ and not falling", len(S)))
cut_script = S[(S.implied < 21) | (S.spread > 7)]
S = S[(S.implied >= 21) & (S.spread <= 7)]; steps.append(("Team expected to score 21+, not a big underdog", len(S)))
S = S.assign(wind=[wind(g) for g in S.game_id])
windy = S.pos.isin(["WR", "TE"]) & (S.wind.fillna(0) >= 15)
S = S[~windy]; steps.append(("No pass catchers in 15+ mph wind", len(S)))
S = S.assign(score=S.vb + 0.03 * (S.dvp > .15) - 0.03 * (S.dvp < -.15)).sort_values("score", ascending=False)
NO_LIMIT = os.environ.get("TD_LIMIT") != "1"  # default: no per-team cap or 20-pick max (set TD_LIMIT=1 to cap)
if not NO_LIMIT:
    S = S.groupby("team", sort=False).head(2); steps.append(("Max 2 per team (spread across games)", len(S)))
if len(S) < MIN_PICKS and len(cut_script):  # top up from the last cut so the card isn't empty
    S = pd.concat([S, cut_script.sort_values("vb", ascending=False).head(MIN_PICKS - len(S))])
S = S if NO_LIMIT else S.head(MAX_PICKS); steps.append(("Card (best first)", len(S)))

os.makedirs(f"{HERE}/picks", exist_ok=True)
S.assign(stake=STAKE)[["game_id", "kick", "pid", "name", "pos", "team", "opp", "best", "bestbook", "p", "pb", "vb",
                       "implied", "snap", "dvp", "stake"]].to_csv(f"{HERE}/picks/{SEASON}_wk{WEEK:02d}.csv", index=False)

ET = ZoneInfo("America/New_York")
lines = [f"🏈 **Anytime TD card · Week {WEEK}** · {len(S)} picks · ${STAKE} each (${STAKE*len(S)} total)",
         "_Fun money: long odds, lots of misses, a hit or two pays the card. Not a proven edge yet, graded every Monday._",
         "_Price shown is the best of the sportsbooks and Kalshi (Kalshi after its fee). Kalshi usually has a smaller cut on TDs._", ""]
for r in S.itertuples():
    k = datetime.strptime(r.kick, "%Y-%m-%d %H:%M").strftime("%a %-I:%M %p")
    why = [f"{r.snap*100:.0f}% snaps", f"team total {r.implied:.1f}"]
    if r.dvp > .15:
        why.append(f"soft D vs {r.pos}s")
    if r.bestbook != "kalshi" and pd.notna(r.k_p):
        why.append(f"Kalshi {amer(r.k_p)}")
    lines.append(f"**{r.name}** ({r.team} vs {r.opp}, {k}) · **{int(r.best):+d}** @ {BOOKS.get(r.bestbook, r.bestbook)} · "
                 f"fair {amer(r.pb)} · {', '.join(why)}")
# 3+ picks from one team usually means we and the books disagree about WHO scores there, not how much: say so
notes = []
for tm, grp in S.groupby("team"):
    if len(grp) < 3:
        continue
    fav = C[C.team == tm].sort_values("mkt", ascending=False).iloc[0]
    best = grp.sort_values("vb", ascending=False).iloc[0]
    notes.append(f"⚠️ **{len(grp)} {tm} picks:** books see {tm}'s TDs going to **{fav['name']}** ({int(fav.best):+d}, "
                 f"{fav.mkt*100:.0f}%) while we have him at {fav.p*100:.0f}% and spread them to the others. "
                 f"If you only want one {tm}: **{best['name']}**.")
if notes:
    lines += [""] + notes
open(f"{HERE}/discord_card.txt", "w").write("\n".join(lines) + "\n")
print("\n".join(f"{n:4}  {lab}" for lab, n in steps))
print("\n".join(lines))
