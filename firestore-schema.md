# Chalk Talk — Firestore Schema

Reuses the existing "Dynasty Dashboard" Firebase project. All collections below are new,
namespaced clearly so nothing collides with Dynasty's existing data.

**Write path: a scheduled + manually-triggered GitHub Action, not Cloud Run.** Earlier drafts
of this doc assumed a deployed Cloud Run service. That's replaced by the same pattern already
proven on CFBModel: `.github/workflows/weekly-update.yml` runs `pipeline/weekly_update.py`
(via `firebase-admin`, a real service-account key stored as a GitHub Actions secret) on a
cron schedule AND on-demand via the Actions tab's "Run workflow" button -- so a fresh odds
pull whenever you want one doesn't need a code push, just a manual trigger. `ChalkTalk.html`
is a static page that reads everything below live from Firestore on load -- editing the site
itself (layout/features) is the only reason to touch Netlify going forward; data updates never
touch it.

## Collections

### `ratings/{team}`
Current-week snapshot, one doc per team (doc ID = team abbreviation, e.g. `BUF`).
```json
{
  "team": "BUF",
  "off_pts": 4.2,
  "def_pts": -1.8,
  "overall_pts": 6.0,
  "st_pts": 0.3,
  "havoc_pts": 2.1,
  "move": 2,
  "week": 10,
  "season": 2026,
  "updated_at": "2026-11-04T12:00:00Z"
}
```

### `ratings_history/{team}`
One doc per team, holding the full walk-forward season history as an array (small enough
— 18 weeks × 5 fields — that a single doc with an array beats a subcollection here).
```json
{
  "team": "BUF",
  "weeks": [
    {"week": 1, "overall_pts": 5.1, "off_pts": 3.0, "def_pts": -2.1, "st_pts": 0.1, "havoc_pts": 1.8},
    { "...": "..." }
  ]
}
```

### `games/{weekId}`
One doc per week (doc ID = `"2026-wk10"`), array of this week's matchups.
```json
{
  "season": 2026, "week": 10,
  "games": [
    {"id": "dolphins-bills", "away": "Miami Dolphins", "home": "Buffalo Bills",
     "model": -3.5, "market": -3.0, "blurb": "..."}
  ]
}
```

### `books/{gameId}`
Per-book price table for one game (doc ID matches the `id` field in `games`).
```json
{"books": [{"book": "draftkings", "home_pt": -3.0, "home_odds": -110, "away_odds": -110}]}
```

### `closing_results/{gameId}`
Final line + score once a game is over.
```json
{"close_home": -3.0, "away_score": 17, "home_score": 24}
```

### `weather/{gameId}`
```json
{"roof": "outdoor", "temp_high": 45, "temp_low": 32, "wind_mph": 12, "precip_pct": 20}
```

### `fantasy_rosters/{leagueId}`
One doc per fantasy league (doc IDs: `sleeper_breezebags`, `espn_bblgrimass`, `yahoo_ddc`) --
the pristine base snapshot, same shape as the old `FANTASY_ROSTERS_BASE` JS const. Written by
hand (a one-time seed script, re-run only when you want to re-sync a whole roster from
scratch) since ESPN/Yahoo have no real API to pull from -- Sleeper's league is real-API-
pullable and could be kept current by the Action later, but isn't yet.
```json
{
  "platform": "sleeper",
  "league_name": "The Big Dynasty League Guys",
  "team_name": "Breezebags",
  "scoring": "half_ppr_superflex",
  "outside_source": null,
  "last_updated": "2026-08-15T00:00:00Z",
  "players": [
    {"name": "Jahmyr Gibbs", "pos": "RB", "team": "DET", "status": "Active", "starter": true}
  ]
}
```

### `fantasy_roster_overrides/{leagueId}` — public client-side write (like `bets`)
Your add/drop/starter-toggle moves on top of the base roster above, one doc per league,
written directly from the browser (the roster editor's Add/Drop/Start-Bench buttons) so an
edit made on one device shows up on every device, replacing the old localStorage-only
version of this. `applyRosterOverrides()` in `ChalkTalk.html` layers this array over the base
roster exactly as it did with localStorage before -- only the storage layer changed.
```json
{"moves": [
  {"type": "add", "player": {"name": "...", "pos": "RB", "team": "...", "status": "Active", "starter": false}},
  {"type": "drop", "key": "playername|team"},
  {"type": "toggle_starter", "key": "playername|team"}
]}
```

### `leaderboards/current`
One doc, overwritten each pipeline run -- QB/WR/RB leaderboards, QB weekly CPOE history, and
per-team top-player snapshot, all recomputed together so they're read together in one `get()`
instead of five separate collection reads.
```json
{
  "qb": [{"player": "M.Willis", "cpoe": 12.9, "epa": 0.43, "n": 101}],
  "wr": [{"player": "T.Kraft", "yac_oe": 4.83, "n": 82}],
  "rb": [{"player": "J.Allen", "epa": 0.455, "n": 96}],
  "qb_history": {"B.Purdy": [{"week": 1, "cpoe": 15.34}]},
  "team_players": {"ARI": {"qb": "J.Brissett", "top_rusher": {"name": "...", "epa": 0.08}, "top_receiver": {"name": "...", "yac_oe": 1.86}, "sack_rate": 0.069, "hit_rate": 0.14}}
}
```

### `fantasy_projections/current`
One doc, overwritten each pipeline run -- replaces the old `FANTASY_PROJECTIONS` JS const
1:1, same `team|lastname` keys.
```json
{"projections": {"jax|lawrence": {"proj": 19.89, "games": 17, "pos": "QB"}}}
```

### `fantasy_weekly/{playerSlug_week}` — not yet wired
One doc per player per week (doc ID e.g. `jahmyr-gibbs_2026-wk10`), meant to hold our
projection alongside the actual result once a week is final and an outside-source comparison
rank. Superseded for now by `fantasy_projections/current` (season-average, opponent-adjusted
client-side) -- revisit if per-week-per-player Firestore history becomes worth the extra
writes.
```json
{
  "player": "Jahmyr Gibbs", "pos": "RB", "team": "DET", "week": 10, "season": 2026,
  "opponent": "GB",
  "our_projection": 16.8,
  "actual_points": null,
  "outside_rank": {"source": "espn_ppr", "rank": 5}
}
```

### `meta/current`
One doc, overwritten each pipeline run -- tells the live site which `games/{weekId}` doc is
"this week" without it having to guess or hold its own copy of the schedule logic.
```json
{"season": 2026, "week": 10, "updated_at": "2026-11-04T12:00:00Z"}
```

### `bets/{betId}` — public client-side write
Bet tracker writes directly from the browser via the quick-track buttons, no backend involved.
```json
{
  "team": "BUF", "book": "draftkings", "side": "home", "week": 10,
  "odds": -110, "line": -3.0, "stake": 25, "result": "pending", "profit": null,
  "created_at": "2026-11-04T18:00:00Z"
}
```

## Security model

Public read on everything. Public write on `bets` (create + update, no delete) and on
`fantasy_roster_overrides` (create + update, no delete -- same shape of trust as `bets`,
your own browser writing your own data with no login system). Everything else is write-only
via the GitHub Action's Admin SDK, which bypasses these client-facing rules entirely -- see
`firestore.rules`. The old `config/pin` collection (meant to gate a Cloud Run HTTP "Run
Update" button) is dropped -- there's no public HTTP endpoint to protect once updates run
through GitHub Actions instead, gated by repo access, not a PIN.
