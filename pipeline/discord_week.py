"""
Discord updates for a weekly market (shared by the NFL and CFB models; keep the copies in sync). Every play appears once; after
that only real changes post. Runs only after an odds pull (the caller decides), never on closing captures.

  1. Week card    first odds run of the week: every starred BET, grouped by game day
  2. New BET      a starred play that wasn't posted yet (or is back after going off the board)
  3. Better price a posted play at a better line, or the same line at least 1% better in payout
  4. Off board    a posted play that is no longer a starred BET anywhere (game not started, market re-pulled)
  5. Game day    first odds run on a game day (not the card's): starred plays still good for today's games
  6. Recap        once last week's posted plays are graded: their results, then the season record

A play here is a dict:
  id         stable identity across pulls: season-week:game:market:team:side (no line/book/price)
  gid, matchup, kick (aware datetime), market, team, side, line, odds, book, ev,
  fair_odds, fair_source, target_odds (worst price that still clears the BET bar), ev_key (tracker key)
  optional: kind (recap group, default "ev"), noun ("BET" / "GO"), line_text (replaces the standard bet line)

State (a dict the caller loads/saves): posted {id: play-as-posted + status/result}, cards [week ids], recaps.
"""
import json
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from odds_books import BOOK_NAMES

ET = ZoneInfo("America/New_York")
BETTER_PAYOUT = 0.01   # same line: post again only when the payout is at least 1% better


def _post(url, text):
    body = json.dumps({"content": text}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": "model-alerts"})
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:  # noqa: BLE001 -- an alert failure must never break the model run
        print(f"[discord] {e}")
        return False
    finally:
        time.sleep(0.6)  # webhooks allow ~5 messages per 2 seconds


def _post_chunks(url, head, blocks, sep="\n"):
    """Post head + blocks, split under Discord's 2,000-character cap. True if every part went through."""
    msgs, cur = [], head
    for b in blocks:
        if len(cur) + len(b) + len(sep) > 1900:
            msgs.append(cur)
            cur = b
        else:
            cur += sep + b
    msgs.append(cur)
    return all([_post(url, m) for m in msgs])


def _dec(american):
    a = float(american)
    return 1 + a / 100 if a > 0 else 1 + 100 / -a


def _pts(p):
    """The line as 'bigger is better' for this side: +3 beats +2.5 on a spread, 44.5 beats 45 on an over."""
    return -p["line"] if p["side"] == "over" else p["line"]


def _odds(a):
    return f"{a:+d}" if isinstance(a, int) else str(a)


def what(p):
    if p["market"] == "spread":
        return f"{p['team']} {p['line']:+g}"
    if p["market"] == "total":
        return f"{p['side'].title()} {p['line']:g}"
    return f"{p['team']} TT {p['side'].title()} {p['line']:g}"


def _bet_line(p):
    if p.get("line_text"):
        return p["line_text"]
    tgt = ""
    if p.get("target_odds") is not None:
        at = (f" (EV {p['target_ev']:+.1%}, bet {p['target_stake']:.2f}%)"
              if p.get("target_ev") is not None and p.get("target_stake") is not None else "")
        tgt = f"\ngood to {_odds(p['target_odds'])}{at}"
    bet = f" · **bet {p['stake']:.2f}%**" if p.get("stake") is not None else ""
    pin = f" · Pinnacle EV {p['ev_pinnacle']:+.1%}" if p.get("ev_pinnacle") is not None else ""
    return (f"★ **{what(p)} {_odds(p['odds'])}** @ {BOOK_NAMES.get(p['book'], p['book'])} · EV {p['ev']:+.1%}{bet}"
            f" · fair {_odds(p['fair_odds'])} ({p['fair_source']}){pin}{tgt}")


def _wk(week_id):
    return "Week " + week_id.rsplit("wk", 1)[-1]


def _when(p):
    return p["kick"].strftime("%a %-I:%M %p ET") if p.get("kick") else ""


def _store(p, status="on"):
    keep = {k: v for k, v in p.items() if k != "kick"}
    keep["kick"] = p["kick"].isoformat() if p.get("kick") else None
    keep.update(status=status, dec=_dec(p["odds"]))
    return keep


def update(url, state, sport, week_id, plays, best_now, repulled, now=None, allow_card=True):
    """plays: every starred BET from this pull. best_now: {id: best offer for that side now, starred or not}.
    repulled(play) -> True if this pull re-priced that play's market (so 'off the board' is a real call)."""
    if not url:
        return state
    now = now or datetime.now(timezone.utc)
    posted = state.setdefault("posted", {})
    cards = state.setdefault("cards", [])
    current = {p["id"]: p for p in plays if not p.get("kick") or p["kick"] > now}

    if week_id not in cards and not allow_card:  # e.g. a game-day run before the week's card: post plays singly
        cards.append(week_id)
    if week_id not in cards:
        days = {}
        for p in sorted(current.values(), key=lambda p: (p.get("kick") or now, p["matchup"])):
            day = p["kick"].strftime("%a %b %-d") if p.get("kick") else "TBD"
            days.setdefault(day, []).append(f"{p['matchup']} · {p['kick'].strftime('%-I:%M %p') if p.get('kick') else ''}\n"
                                            f"{_bet_line(p)}")
        head = f"{sport} **{_wk(week_id)} card** · {len(current)} play{'s' if len(current) != 1 else ''}"
        if not current:
            head += "\nNo plays yet. New ones post here as lines move."
        blocks = [f"\n__**{d}**__\n" + "\n\n".join(items) for d, items in days.items()]
        if _post_chunks(url, head + "\n_Each play posts once; after this only new plays, better numbers and "
                                   "plays that come off the board._", blocks):
            cards.append(week_id)
            state["gameday"] = now.astimezone(ET).date().isoformat()
            for p in current.values():
                posted[p["id"]] = _store(p)
        state["cards"] = cards[-40:]
        return state

    just = set()  # posted in this run: not repeated in today's list
    for pid, p in current.items():
        old = posted.get(pid)
        if old is None or old.get("status") == "off":
            tag = "🔁 **Back on**" if old else f"🆕 **New {p.get('noun', 'BET')}**"
            if _post(url, f"{sport} {tag} · {p['matchup']} · {_when(p)}\n{_bet_line(p)}"):
                posted[pid] = _store(p)
                just.add(pid)
            continue
        same_line_better = _pts(p) == _pts(old) and _dec(p["odds"]) >= old["dec"] * (1 + BETTER_PAYOUT)
        line_better = _pts(p) > _pts(old) and _dec(p["odds"]) >= old["dec"] * 0.98
        if same_line_better or line_better:
            was = f"{what(old)} {_odds(old['odds'])} @ {BOOK_NAMES.get(old['book'], old['book'])}"
            if _post(url, f"{sport} 📈 **Better number** · {p['matchup']} · {_when(p)}\n{_bet_line(p)}\n_was {was}_"):
                posted[pid] = _store(p)
                just.add(pid)

    # game-day list: first odds run on a day with games (not the card's day) -- what's still good today
    today = now.astimezone(ET).date()
    if state.get("gameday") != today.isoformat():
        todays = [p for pid, p in current.items() if p.get("kick") and p["kick"].astimezone(ET).date() == today
                  and posted.get(pid, {}).get("status") == "on" and pid not in just]
        if todays:
            blocks = [f"{p['matchup']} · {p['kick'].strftime('%-I:%M %p')}\n{_bet_line(p)}"
                      for p in sorted(todays, key=lambda p: (p["kick"], p["matchup"]))]
            if _post_chunks(url, f"{sport} **Still good today · {today.strftime('%a %b %-d')}** · {len(todays)} "
                                 f"BET{'s' if len(todays) != 1 else ''} at the best price right now", blocks, "\n\n"):
                state["gameday"] = today.isoformat()
        else:
            state["gameday"] = today.isoformat()

    for pid, old in posted.items():
        if old.get("status") != "on" or pid in current or not pid.startswith(week_id + ":"):
            continue
        kick = datetime.fromisoformat(old["kick"]) if old.get("kick") else None
        if (kick and kick <= now) or not repulled(old):
            continue
        b = best_now.get(pid)
        now_txt = (b["now_text"] if b and b.get("now_text") else
                   f"best now {what(b)} {_odds(b['odds'])} @ {BOOK_NAMES.get(b['book'], b['book'])}, EV {b['ev']:+.1%}"
                   if b else "no longer offered")
        if _post(url, f"{sport} ❌ **Off the board** · {old['matchup']}\n~~{what(old)} {_odds(old['odds'])}~~ "
                      f"no longer a {old.get('noun', 'BET')} ({now_txt}). Skip it if you haven't bet it."):
            old["status"] = "off"
    return state


KIND_LABEL = {"ev": "★ EV plays", "model": "🎯 Model totals (GO)"}


def recap(url, state, sport, week_id, graded, season_prefix):
    """graded: {ev_key: tracker play with result/units/close_ev} for that week. Posts once when every play we
    posted that week is graded; also stores each result so the season line counts only what we posted."""
    if not url or week_id in state.setdefault("recaps", []):
        return state
    posted = state.get("posted", {})
    week = [p for pid, p in posted.items() if pid.startswith(week_id + ":")]
    if not week:
        return state
    for p in week:
        g = graded.get(p.get("ev_key"))
        if g and g.get("result"):
            p.update(result=g["result"], units=g.get("units", 0.0), close_ev=g.get("close_ev"))
    if any("result" not in p for p in week):
        return state

    def rec(ps):
        w, l, pu = (sum(p["result"] == r for p in ps) for r in ("win", "loss", "push"))
        clv = [p["close_ev"] for p in ps if p.get("close_ev") is not None]
        beat = f" · beat close {100 * sum(c > 0 for c in clv) / len(clv):.0f}%" if clv else ""
        return f"**{w}-{l}{f'-{pu}' if pu else ''}** · **{sum(p['units'] for p in ps):+.2f}u**{beat}"

    icon = {"win": "✅", "loss": "❌", "push": "➖"}
    lines = [f"{icon[p['result']]} {p['matchup']} · **{what(p)} {_odds(p['odds'])}** @ "
             f"{BOOK_NAMES.get(p['book'], p['book'])} · {p['units']:+.2f}u"
             + (f" · close EV {p['close_ev']:+.1%}" if p.get("close_ev") is not None else "")
             + (" _(went off the board)_" if p.get("status") == "off" else "")
             for p in sorted(week, key=lambda p: p.get("kick") or "")]
    season = [p for pid, p in posted.items() if pid.startswith(season_prefix) and "result" in p]
    kinds = sorted({p.get("kind", "ev") for p in season})
    head = f"📊 {sport} **{_wk(week_id)} recap** · {rec(week)} _(1u risked per play)_"
    tail = [f"Season (posted plays): {rec(season)}"]
    if len(kinds) > 1:  # separate records for each kind of play (CFB: EV plays vs model totals)
        tail += [f"{KIND_LABEL.get(k, k)}: week {rec([p for p in week if p.get('kind', 'ev') == k]) if any(p.get('kind', 'ev') == k for p in week) else '-'}"
                 f" · season {rec([p for p in season if p.get('kind', 'ev') == k])}" for k in kinds]
    if _post_chunks(url, head, lines + tail):
        state["recaps"] = (state["recaps"] + [week_id])[-40:]
    return state
