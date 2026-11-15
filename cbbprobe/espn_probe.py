import json, urllib.request
from collections import Counter
url = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"
try:
    d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "nhl-model"}), timeout=20))
    print("keys", list(d.keys()))
    teams = d.get("injuries", [])
    print(len(teams), "teams;", sum(len(t.get("injuries", [])) for t in teams), "entries")
    print("statuses", Counter(i.get("status") for t in teams for i in t.get("injuries", [])))
    t = teams[0]; print("team keys", list(t.keys()), t.get("displayName"))
    print(json.dumps(t["injuries"][0])[:900])
except Exception as e:
    print("ERR", e)
