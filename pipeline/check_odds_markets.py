"""One-off diagnostic: does our real Odds API plan actually include team_totals / alternate_spreads
/ player props, or just the core spreads market we already use? Prints only HTTP status codes and
error messages -- never the API key, never full odds payloads."""
import os
import urllib.request
import urllib.error
import json

API_KEY = os.environ["ODDS_API_KEY"]

# Step 1: get the real event list (this endpoint is part of the core plan, same as odds/)
events_url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/?apiKey={API_KEY}"
with urllib.request.urlopen(events_url, timeout=15) as resp:
    events = json.loads(resp.read())
    headers_seen = dict(resp.headers)

print(f"Found {len(events)} real upcoming/live NFL events.")
remaining = headers_seen.get('x-requests-remaining')
used = headers_seen.get('x-requests-used')
print(f"Real account quota -- used: {used}, remaining: {remaining}")

if not events:
    print("No events available to test against right now.")
    raise SystemExit(0)

event_id = events[0]["id"]
print(f"Testing against real event: {events[0]['away_team']} @ {events[0]['home_team']} ({event_id})")

def try_market(market):
    url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{event_id}/odds?apiKey={API_KEY}&regions=us&markets={market}&oddsFormat=american"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
            n_books = len(data.get("bookmakers", []))
            has_market = any(m["key"] == market for bm in data.get("bookmakers", []) for m in bm.get("markets", []))
            print(f"  {market:25s} -> HTTP 200, {n_books} bookmaker(s) responded, market actually present: {has_market}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')[:200]
        print(f"  {market:25s} -> HTTP {e.code}: {body}")
    except Exception as e:
        print(f"  {market:25s} -> ERROR: {e}")

for market in ["spreads", "h2h", "totals", "team_totals", "alternate_spreads", "alternate_totals", "player_pass_yds"]:
    try_market(market)
