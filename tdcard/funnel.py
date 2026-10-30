"""
Weekly anytime-TD card: filter the priced player list down to 5-20 picks in visible steps, then write the Discord
message and the picks file used for Monday's grading.

python3 tdcard/funnel.py <season> <week>     (reads tdcard/card_<season>_wk<week>.csv from card.py)
Writes tdcard/discord_card.txt and tdcard/picks/<season>_wk<week>.csv. Fun money: no proven edge.
"""
import json, os, sys, urllib.request
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
         "betonlineag": "BetOnline", "hardrockbet": "Hard Rock", "bovada": "Bovada"}
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
         "_Fun money: long odds, lots of misses, a hit or two pays the card. Not a proven edge yet, graded every Monday._", ""]
for r in S.itertuples():
    k = datetime.strptime(r.kick, "%Y-%m-%d %H:%M").strftime("%a %-I:%M %p")
    why = [f"{r.snap*100:.0f}% snaps", f"team total {r.implied:.1f}"]
    if r.dvp > .15:
        why.append(f"soft D vs {r.pos}s")
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
