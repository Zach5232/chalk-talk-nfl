# CBB price plays

College basketball **price plays**: any sportsbook or exchange price that beats the sharp fair line
(a weighted consensus led by Pinnacle and the exchanges). **Log-only** for now: plays are graded against the
closing line and the final score before anything is posted.

- `cbb/cbb.py pull`: Odds API pull (13 books, 6 credits), log starred plays (2%+ vs the consensus and vs Pinnacle)
- `cbb/cbb.py fast`: Kalshi moneylines every 10 minutes vs the last fair line (free)
- `cbb/cbb.py grade`: results + closing-line value

Secrets (shared with the NFL pipeline): `ODDS_API_KEY`, `FIREBASE_SERVICE_ACCOUNT`. Data lives in Firestore (`cbb_state`, `cbb_plays`), not in this repo.
