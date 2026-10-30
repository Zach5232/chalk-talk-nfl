"""Grade the TD card picks for a week from nflverse weekly stats; write the Discord results message.
python3 tdcard/grade.py <season> <week>  -> tdcard/discord_results.txt, updates tdcard/picks/<season>_wk<week>.csv"""
import glob, os, sys
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SEASON, WEEK = int(sys.argv[1]), int(sys.argv[2])
dec = lambda a: 1 + a / 100 if a > 0 else 1 + 100 / -a
w = pd.read_parquet(f"{HERE}/data/weekly_{SEASON}.parquet")
w = w[(w.season == SEASON) & (w.week == WEEK)]
tds = dict(zip(w.player_id, w.rushing_tds.fillna(0) + w.receiving_tds.fillna(0)))
path = f"{HERE}/picks/{SEASON}_wk{WEEK:02d}.csv"
P = pd.read_csv(path)
# a player with no stat line this week may have been inactive (bet voided); check snaps when available
snap = f"{HERE}/data/snaps_{SEASON}.parquet"
played = set(w.player_id)
P["result"] = ["win" if tds.get(pid, 0) > 0 else ("loss" if pid in played else "void?") for pid in P.pid]
P["profit"] = [r.stake * (dec(r.best) - 1) if r.result == "win" else (-r.stake if r.result == "loss" else 0.0) for r in P.itertuples()]
P.to_csv(path, index=False)

season = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(f"{HERE}/picks/{SEASON}_wk*.csv"))])
season = season[season.result.isin(["win", "loss"])] if "result" in season else season
hits = P[P.result == "win"]
lines = [f"📊 **TD card results · Week {WEEK}** · {len(hits)} of {int((P.result != 'void?').sum())} scored · "
         f"**{P.profit.sum():+.0f}$** on ${P.stake[P.result != 'void?'].sum():.0f}"]
for r in hits.itertuples():
    lines.append(f"✅ {r.name} {int(r.best):+d} (+${r.profit:.0f})")
misses = P[P.result == "loss"].name.tolist()
if misses:
    lines.append("❌ " + ", ".join(misses))
voids = P[P.result == "void?"].name.tolist()
if voids:
    lines.append("➖ no stat line (likely inactive, bet void): " + ", ".join(voids))
if len(season):
    lines.append(f"Season: {int((season.result == 'win').sum())}/{len(season)} hit · {season.profit.sum():+.0f}$ "
                 f"on ${season.stake.sum():.0f} ({season.profit.sum()/season.stake.sum()*100:+.0f}%)")
open(f"{HERE}/discord_results.txt", "w").write("\n".join(lines) + "\n")
print("\n".join(lines))
