"""
Real, automated QB-starter detection -- runs on its OWN frequent, free cron (see
.github/workflows/qb-status-check.yml), decoupled from the once-a-week weekly_update.py run.

Why a separate script/workflow instead of just running weekly_update.py more often: this needs
zero Odds API calls (just two free nflverse files -- depth_charts, injuries -- plus the current
season's real play-by-play, all already-free downloads), so it can run every few hours all
week for real-time coverage of a mid-week injury/starter change, while the heavy weekly
pipeline (ratings refit + real paid odds pulls) stays on its Tuesday cadence. The real problem
this solves: a starter change on, say, a Wednesday used to just sit unflagged on the dashboard
until someone noticed and typed it in by hand -- now it's caught automatically within a few
real hours by this job, with the actual per-player EPA penalty computed the same real,
personal-track-record way qb_personal_penalty already does for a manual entry.

See build_qb_depth_chart_status() in weekly_update.py for the real detection logic itself
(this script is just the fetch -> compute -> write wrapper, matching capture_closing_lines.py's
existing pattern for a frequent, lightweight satellite job).
"""
import json
import os

from weekly_update import (
    SEASON, build_qb_depth_chart_status, fetch_pbp, get_firestore_client, _FIREBASE_AVAILABLE,
)
if _FIREBASE_AVAILABLE:
    from weekly_update import firestore


def run():
    pbp_path = fetch_pbp(SEASON)
    if not pbp_path:
        print(f"No real {SEASON} play-by-play published yet -- nothing to cross-check against, skipping.")
        return

    status = build_qb_depth_chart_status(SEASON, pbp_path)
    flagged = {t: s for t, s in status.items() if s["flag"]}
    print(f"{len(status)} teams checked, {len(flagged)} real auto-detected change(s): {', '.join(flagged) or 'none'}")

    cred_path = os.environ.get("FIREBASE_CREDENTIALS_PATH") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    fdb = get_firestore_client(cred_path) if (_FIREBASE_AVAILABLE and cred_path) else None
    if not fdb:
        print("No Firestore credentials configured -- printing only, nothing written.")
        print(json.dumps(status, indent=1))
        return

    fdb.collection("qb_depth_chart_status").document("current").set({
        "season": SEASON,
        "teams": status,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
    print("Written to qb_depth_chart_status/current.")


if __name__ == "__main__":
    run()
