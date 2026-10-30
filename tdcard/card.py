"""Anytime-TD card for an upcoming week (v3 model: usage shares + injuries + snap share + opponent vs position + QB
goal-line, coefficients fit on all completed 2023-26 games). Prices from an odds JSON (live pull or saved snapshot).

python3 td_card.py <season> <week> <odds.json>   -> card.md + card.csv
"""
import json, re, sys, urllib.request
import numpy as np, pandas as pd

import os
D = os.environ.get("TD_DIR", os.path.dirname(os.path.abspath(__file__)))  # data/ under here
SEASON, WEEK, ODDS = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
HALF = 4.0


def norm(n):
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", str(n).lower())
    return re.sub(r"[^a-z]", "", n)


dec = lambda a: 1 + a / 100 if a > 0 else 1 + 100 / -a
games = pd.read_csv(f"{D}/data/games.csv.gz")
slate = games[(games.season == SEASON) & (games.week == WEEK) & games.result.isna()]
cols = ["game_id", "season", "week", "season_type", "posteam", "play_type", "yardline_100", "rusher_player_id",
        "receiver_player_id", "rush_touchdown", "pass_touchdown", "two_point_attempt"]
pbp = pd.concat([pd.read_parquet(f"{D}/data/pbp_{y}.parquet", columns=cols) for y in (SEASON - 1, SEASON)], ignore_index=True)
pbp = pbp[pbp.season_type == "REG"]
runs = pbp[(pbp.play_type == "run") & pbp.rusher_player_id.notna() & (pbp.two_point_attempt != 1)]
tg = pbp[(pbp.play_type == "pass") & pbp.receiver_player_id.notna() & (pbp.two_point_attempt != 1)]
opp = pd.concat([runs.assign(pid=runs.rusher_player_id, td=runs.rush_touchdown.fillna(0)),
                 tg.assign(pid=tg.receiver_player_id, td=tg.pass_touchdown.fillna(0))])
opp["rz"], opp["i10"], opp["touch"] = (opp.yardline_100 <= 20).astype(int), (opp.yardline_100 <= 10).astype(int), 1
pg = opp.groupby(["game_id", "season", "week", "posteam", "pid"], as_index=False).agg(
    touches=("touch", "sum"), rz=("rz", "sum"), i10=("i10", "sum"), td=("td", "sum"))
team = pg.groupby(["game_id", "posteam"]).agg(t_rz=("rz", "sum"), t_i10=("i10", "sum"), t_touch=("touches", "sum"), team_tds=("td", "sum"))
pg = pg.join(team, on=["game_id", "posteam"])

# usage shares as of this week (same formula as the backtest)
past = pg.copy()
past["age"] = np.where(past.season == SEASON, WEEK - past.week, 99)
recent_in_season = (past.season == SEASON).sum() > 2000
if recent_in_season:
    past = past[past.season == SEASON]
past["w"] = np.where(past.age == 99, 0.35, 0.5 ** ((past.age - 1) / HALF))
agg = past.assign(wrz=past.w * past.rz, wi10=past.w * past.i10, wtd=past.w * past.td, wtouch=past.w * past.touches,
                  wtrz=past.w * past.t_rz, wti10=past.w * past.t_i10, wttd=past.w * past.team_tds,
                  wttouch=past.w * past.t_touch).groupby("pid").sum(numeric_only=True)
share = (0.35 * (agg.wi10 + .3) / (agg.wti10 + 3) + 0.25 * (agg.wrz + .5) / (agg.wtrz + 5)
         + 0.25 * (agg.wtd + .3) / (agg.wttd + 3) + 0.15 * agg.wtouch / agg.wttouch.clip(lower=1))
lastteam = past.sort_values(["season", "week"]).groupby("pid").posteam.last()
recent = past[(past.season == SEASON) & (past.age <= 3)].pid.unique()

# team TDs per implied point, from all completed games
hist = []
for y in (SEASON - 2, SEASON - 1, SEASON):
    g = games[(games.season == y) & games.result.notna() & (games.game_type == "REG")]
    for x in g.itertuples():
        hist += [((x.total_line + x.spread_line) / 2, x.home_score), ((x.total_line - x.spread_line) / 2, x.away_score)]
hp = np.array(hist, float)
b_pts, a_pts = np.polyfit(hp[:, 0], hp[:, 1] / 7.0 * 0.92, 1)  # ~points -> offensive TDs

weekly = pd.concat([pd.read_parquet(f"{D}/data/weekly_{y}.parquet") for y in (SEASON - 1, SEASON)], ignore_index=True)
info = weekly.sort_values(["season", "week"]).drop_duplicates("player_id", keep="last").set_index("player_id")
rows = []
for x in slate.itertuples():
    if pd.isna(x.spread_line):
        continue
    for tm, opp_tm, implied in ((x.home_team, x.away_team, (x.total_line + x.spread_line) / 2),
                                (x.away_team, x.home_team, (x.total_line - x.spread_line) / 2)):
        for pid in lastteam[(lastteam == tm) & lastteam.index.isin(recent)].index:
            if pid not in share.index or pid not in info.index:
                continue
            lam = max(a_pts + b_pts * implied, 0.3) * share[pid]
            rows.append(dict(game_id=x.game_id, kick=f"{x.gameday} {x.gametime}", team=tm, opp=opp_tm, implied=implied,
                             total=x.total_line, spread=x.spread_line if tm == x.home_team else -x.spread_line,
                             pid=pid, name=info.player_display_name[pid], pos=str(info.position[pid]).replace("FB", "RB"),
                             lam=lam))
C = pd.DataFrame(rows)

# injuries (latest report for this week)
inj = pd.read_parquet(f"{D}/data/inj_{SEASON}.parquet")
inj = inj[(inj.week == WEEK)]
rank = {"Out": 3, "Doubtful": 2, "Questionable": 1}
inj = inj[inj.report_status.isin(rank)].assign(r=lambda z: z.report_status.map(rank)).sort_values("r").drop_duplicates("gsis_id", keep="last")
C["status"] = C.pid.map(inj.set_index("gsis_id").report_status)
out = C.status.isin(["Out", "Doubtful"])
C["lam2"] = C.lam
for _, grp in C.groupby(["game_id", "team", "pos"]):
    lost = grp.loc[out[grp.index], "lam"].sum()
    healthy = grp.index[~out[grp.index]]
    if lost > 0 and len(healthy):
        C.loc[healthy, "lam2"] += 0.8 * lost * C.loc[healthy, "lam"] / C.loc[healthy, "lam"].sum()
C["boosted"] = C.lam2 > C.lam * 1.05
C.loc[C.status == "Questionable", "lam2"] *= 0.88
C["p2"] = 1 - np.exp(-C.lam2)
C = C[~out].copy()

# v3 features
sn = pd.read_parquet(f"{D}/data/snaps_{SEASON}.parquet")
sn = sn[sn.game_type == "REG"].assign(key=sn.player.map(norm))
ids = weekly.assign(key=weekly.player_display_name.map(norm))[["team", "key", "player_id"]].drop_duplicates(["team", "key"])
sn = sn.merge(ids, on=["team", "key"]).sort_values("week")
snapf = {}
for pid, s in sn.groupby("player_id"):
    s = s.tail(6); w = 0.7 ** np.arange(len(s))[::-1]
    snapf[pid] = float(np.sum(w * s.offense_pct) / w.sum())
snap_last = sn.groupby("player_id").offense_pct.last()
C["snap"] = C.pid.map(snapf)
C["snap"] = C.snap.fillna(C.groupby("pos").snap.transform("median")).fillna(0.5)
C["snap_last"] = C.pid.map(snap_last)
wk = weekly[weekly.season == SEASON].assign(tdn=lambda z: z.rushing_tds.fillna(0) + z.receiving_tds.fillna(0),
                                            pos=lambda z: z.position.replace({"FB": "RB"}))
allowed = wk.groupby(["opponent_team", "pos"]).tdn.sum()
ngames = wk.groupby("opponent_team").week.nunique()
league = wk.groupby("pos").tdn.sum() / wk.groupby("opponent_team").week.nunique().sum()
C["dvp"] = [np.log((allowed.get((o, p), 0) + 6 * league.get(p, .5)) / ((ngames.get(o, 0) + 6) * league.get(p, .5)))
            for o, p in zip(C.opp, C.pos)]
gl = pbp[(pbp.season == SEASON) & (pbp.play_type == "run") & (pbp.yardline_100 <= 5) & (pbp.two_point_attempt != 1)].copy()
gl["qb"] = (gl.rusher_player_id.map(info.position) == "QB").astype(int)
glr = gl.groupby("posteam").agg(qb=("qb", "sum"), n=("qb", "size"))
C["qbgl"] = [(glr.qb.get(t, 0) + 1.5) / (glr.n.get(t, 0) + 10) for t in C.team]

# coefficients: fit on all completed games (2023-26) from the v3 backtest table
T = pd.read_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "train_v3.csv.gz"))
T = T[T.season >= 2023]
lg = lambda p: np.log(np.clip(p, 1e-4, 1 - 1e-4) / (1 - np.clip(p, 1e-4, 1 - 1e-4)))


def feats(X):
    rb = (X.pos == "RB").astype(float); te = (X.pos == "TE").astype(float); qb = (X.pos == "QB").astype(float)
    return np.column_stack([np.ones(len(X)), lg(X.p2), X.snap, X.dvp, X.qbgl * rb, X.qbgl * qb, rb, te, qb])


Xt, yt = feats(T), T.scored.values.astype(float)
bta = np.zeros(Xt.shape[1]); bta[1] = 1.0
for _ in range(60):
    p = 1 / (1 + np.exp(-(Xt @ bta))); W = p * (1 - p)
    g = Xt.T @ (yt - p) - np.r_[0, bta[1:] - np.r_[1, np.zeros(len(bta) - 2)]]
    H = (Xt * W[:, None]).T @ Xt + np.diag(np.r_[0, np.ones(len(bta) - 1)])
    bta = bta + np.linalg.solve(H + 1e-9 * np.eye(len(bta)), g)
C["p"] = 1 / (1 + np.exp(-(feats(C) @ bta)))

# prices
book = {}
for gm in json.load(open(ODDS)):
    for bk in gm.get("bookmakers", []):
        for mk in bk.get("markets", []):
            for o in mk.get("outcomes", []):
                if o.get("name") == "Yes" and o.get("description"):
                    book.setdefault((gm["game_id"], norm(o["description"])), []).append((o["price"], bk["key"]))
C["key"] = C.name.map(norm)
C["best"] = [max(book[(g, k)], key=lambda z: dec(z[0]))[0] if (g, k) in book else np.nan for g, k in zip(C.game_id, C.key)]
C["bestbook"] = [max(book[(g, k)], key=lambda z: dec(z[0]))[1] if (g, k) in book else "" for g, k in zip(C.game_id, C.key)]
C["nbooks"] = [len(book.get((g, k), [])) for g, k in zip(C.game_id, C.key)]
C["mkt"] = [np.mean([1 / dec(z[0]) for z in book[(g, k)]]) if (g, k) in book else np.nan for g, k in zip(C.game_id, C.key)]
C = C[C.best.notna()].copy()
C["value"] = C.p * C.best.map(dec) - 1
C["fair"] = C.p.map(lambda q: f"{(1/q-1)*100:+.0f}" if q < .5 else f"{-100/(1/q-1):.0f}")
C["dvp_note"] = C.dvp.map(lambda v: "soft D" if v > .15 else ("tough D" if v < -.15 else ""))
C.sort_values("value", ascending=False).to_csv(f"{D}/card_{SEASON}_wk{WEEK}.csv", index=False)
raise SystemExit(0)  # the funnel (funnel.py) picks and formats the card

pick = C[(C.value >= .10) & (C.best >= 200) & (C.best < 600) & ~C.boosted].sort_values("value", ascending=False)
L = [f"# Anytime TD card · {SEASON} Week {WEEK}\n",
     "Fun money only: no proven edge yet. Model = v3 (usage, snaps, injuries, opponent, QB goal-line).",
     "Shortlist rule: model value >= 10% at the best price, priced +200 to +600, not an injury-replacement bump.\n",
     "| Player | Team | vs | Kick | Best price | Book | Model | Fair | Value | Team implied | Snap% (avg/last) | Notes |",
     "|---|---|---|---|---|---|---|---|---|---|---|---|"]
for r in pick.itertuples():
    notes = ", ".join(x for x in (r.dvp_note, f"status {r.status}" if isinstance(r.status, str) else "") if x)
    L.append(f"| {r.name} | {r.team} | {r.opp} | {r.kick} | {int(r.best):+d} | {r.bestbook} | {r.p*100:.0f}% | {r.fair} | {r.value*100:+.0f}% | "
             f"{r.implied:.1f} | {r.snap*100:.0f}% / {r.snap_last*100 if pd.notna(r.snap_last) else float('nan'):.0f}% | {notes} |")
L.append(f"\n{len(pick)} on the shortlist · {len(C)} priced players modeled")
open(f"{D}/card_{SEASON}_wk{WEEK}.md", "w").write("\n".join(L) + "\n")
print("\n".join(L))
