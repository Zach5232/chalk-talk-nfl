"""Post a text file to the NFL Discord webhook, split under Discord's 2,000-character limit."""
import json, os, sys, time, urllib.request

url, text = os.environ.get("DISCORD_WEBHOOK_URL", ""), open(sys.argv[1]).read().strip()
if not url:
    print("no DISCORD_WEBHOOK_URL -- not posting"); print(text); raise SystemExit(0)
parts, cur = [], ""
for line in text.split("\n"):
    if len(cur) + len(line) + 1 > 1900:
        parts.append(cur); cur = line
    else:
        cur = f"{cur}\n{line}" if cur else line
parts.append(cur)
for p in parts:
    req = urllib.request.Request(url, data=json.dumps({"content": p}).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "model-alerts"})
    urllib.request.urlopen(req, timeout=15); time.sleep(1)
print(f"posted {len(parts)} message(s)")
