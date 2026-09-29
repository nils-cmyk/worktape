#!/usr/bin/env python3
"""WorkTape watcher: weekly and monthly review of recurring workflows, scored for how much Claude could take over.

Reads the daily classifier's output (reports/<day>/day.json), aggregates per workflow, asks Claude to score
each of the biggest workflows against a fixed rubric, and writes
  ~/WorkTape/reports/weekly/<YYYY-Www>.html     or   ~/WorkTape/reports/monthly/<YYYY-MM>.html
  ~/WorkTape/data/watch-history.csv             one row per workflow per period, for trends

Usage:
  watcher.py weekly                  # the 7 days ending yesterday
  watcher.py monthly                 # the previous calendar month (or month-to-date when run mid-month with --mtd)
  watcher.py weekly --end 2026-09-29 # a window ending on a given day (inclusive)
"""

import csv
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import classify as C  # noqa: E402

HISTORY = C.DATA / "watch-history.csv"
WATCH_CONFIG = C.ROOT / "watch.json"

WATCH_DEFAULTS = {
    "topN": 15,
    "model": "opus",
    "activitiesPerWorkflow": 14,
    # What an agent could reach today. Edit when a connection is added or removed.
    "agentTools": [   # what an agent could reach on this Mac; edit to match your setup
        "Claude Code on this Mac (files, shell, git, local repos)",
        "Claude skills and scheduled tasks",
        "Connectors enabled in Claude (e.g. Slack, Gmail, Calendar, Drive, Notion)",
        "Browser automation and computer use for apps without an API",
    ],
    # Anthropic's prompting guides the bottleneck diagnosis is judged against (refreshed weekly, cached locally)
    "guides": [
        "https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5-5.md",
        "https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices.md",
    ],
    "promptsPerWorkflow": 8,
    "rubric": {
        "repeatable": "Same steps each time it recurs",
        "clear_io": "Inputs and a checkable output are well defined",
        "tool_access": "Claude can reach every system the work touches (see tools)",
        "low_judgment": "Little taste or relationship judgment needed (5 = none)",
        "low_risk": "A mistake is cheap and caught before it reaches a client (5 = safe)",
    },
}


def load_watch_config():
    cfg = dict(WATCH_DEFAULTS)
    if WATCH_CONFIG.exists():
        cfg.update(json.loads(WATCH_CONFIG.read_text()))
    else:
        WATCH_CONFIG.write_text(json.dumps(WATCH_DEFAULTS, indent=2))
    return cfg


# ---------- window ----------

def window(kind, end_arg, mtd):
    today = dt.date.today()
    if kind == "weekly":
        end = dt.date.fromisoformat(end_arg) if end_arg else today - dt.timedelta(days=1)
        start = end - dt.timedelta(days=6)
        iso = end.isocalendar()
        return start, end, f"{iso[0]}-W{iso[1]:02d}"
    if end_arg:
        end = dt.date.fromisoformat(end_arg)
        start = end.replace(day=1)
    elif mtd:
        end, start = today, today.replace(day=1)
    else:
        end = today.replace(day=1) - dt.timedelta(days=1)
        start = end.replace(day=1)
    return start, end, f"{start:%Y-%m}"


def days_between(start, end):
    return [(start + dt.timedelta(days=k)).isoformat() for k in range((end - start).days + 1)]


def ensure_daily(days):
    """Run the daily classifier for any day in the window that is missing or has newer recordings."""
    cfg = C.load_config()
    for d in days:
        if d != dt.date.today().isoformat() and (C.VIDEOS / d).exists() and C.stale(d):
            C.log(f"watcher: classifying {d} first")
            try:
                C.process(d, cfg)
            except Exception as e:
                C.log(f"watcher: {d} FAILED {e}")


# ---------- prompting guides ----------

GUIDES = C.DATA / "guides"


def load_guides(wcfg):
    GUIDES.mkdir(parents=True, exist_ok=True)
    texts = []
    for url in wcfg["guides"]:
        path = GUIDES / url.rstrip("/").split("/")[-1]
        if not path.exists() or time.time() - path.stat().st_mtime > 7 * 86400:
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    path.write_bytes(r.read())
            except Exception as e:  # keep the cached copy if the docs site is unreachable
                C.log(f"watcher: could not refresh {url}: {e}")
        if path.exists():
            texts.append(f'<guide source="{url}">\n{path.read_text()}\n</guide>')
    return "\n\n".join(texts)


# ---------- Claude session stats (local, no tokens) ----------

NUDGE = re.compile(r"^\s*(ok(ay)?|yes|yep|y|go+(go)*|go ahead|continue|proceed|do it|sure|next|keep going|carry on|"
                   r"sounds good|great|perfect|done)\W*$", re.I)


def session_stats(path, start, end):
    """What happened in one Claude session inside the window: the user's prompts, how long Claude worked per prompt,
    how long Claude then waited for him, nudges, effort levels, and turns that ended on a question."""
    t0 = dt.datetime.combine(start, dt.time())
    t1 = dt.datetime.combine(end, dt.time()) + dt.timedelta(days=1)
    title, events = "", []   # (time, kind, text, effort, stop)
    with open(path, errors="ignore") as f:
        for line in f:
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                continue
            if j.get("type") == "custom-title":
                title = j.get("customTitle", title)
            if j.get("type") not in ("user", "assistant") or "timestamp" not in j or j.get("isSidechain"):
                continue
            t = C._local(j["timestamp"])
            if not t0 <= t < t1:
                continue
            if j["type"] == "user":
                if "toolUseResult" in j or (j.get("origin") or {}).get("kind") != "human":
                    continue
                txt = C._text(j.get("message"))
                if txt:
                    events.append((t, "human", txt, None, None))
            else:
                m = j.get("message") or {}
                events.append((t, "claude", C._text(m), j.get("effort"), m.get("stop_reason")))
    prompts = [(t, x) for t, k, x, _, _ in events if k == "human"]
    claude_s = user_s = 0.0
    questions = 0
    effort = {}
    for i, (t, k, x, eff, stop) in enumerate(events):
        if k == "claude":
            if eff:
                effort[eff] = effort.get(eff, 0) + 1
            if stop == "end_turn" and x.rstrip().endswith("?"):
                questions += 1
    # per prompt: Claude's working time until its last reply, then the gap until the user's next prompt
    idx = [i for i, e in enumerate(events) if e[1] == "human"] + [len(events)]
    for a, b in zip(idx, idx[1:]):
        replies = [e[0] for e in events[a + 1:b] if e[1] == "claude"]
        if replies:
            claude_s += (replies[-1] - events[a][0]).total_seconds()
            if b < len(events):
                gap = (events[b][0] - replies[-1]).total_seconds()
                user_s += min(gap, 1800)   # a gap over 30 min is a break, not waiting
    return {"title": title, "prompts": prompts, "turns": len(prompts),
            "nudges": sum(1 for _, x in prompts if NUDGE.match(x) or len(x) <= 12),
            "claude_minutes": round(claude_s / 60, 1), "waiting_on_user_minutes": round(user_s / 60, 1),
            "ended_on_question": questions, "effort": effort}


# ---------- aggregate (local, no tokens) ----------

def aggregate(days):
    agg = {}
    for d in days:
        p = C.REPORTS / d / "day.json"
        if not p.exists():
            continue
        for s in json.loads(p.read_text())["segments"]:
            a = agg.setdefault(s["workflow"], {
                "id": s["workflow"], "name": s["workflow_name"], "category": s["category"], "minutes": 0.0,
                "known_minutes": 0.0, "hands_on": 0.0, "sessions": 0, "days": set(), "clients": {},
                "activities": [], "top": [], "claude_sessions": {}})
            a["minutes"] += s["minutes"]
            a["sessions"] += 1
            a["days"].add(d)
            a["clients"][s["client"]] = a["clients"].get(s["client"], 0) + s["minutes"]
            if s.get("hands_on_share") is not None:
                a["known_minutes"] += s["minutes"]
                a["hands_on"] += s["minutes"] * s["hands_on_share"]
            for act in s["activities"]:
                a["activities"].append(f"{d} {s['start'][11:16]} [{s['client']}] {act}")
            a["top"].append({**s, "day": d})
            for x in s.get("sessions", []):
                ss = a["claude_sessions"].setdefault(x["id"], {"id": x["id"], "title": x["title"], "path": x["path"],
                                                         "cwd": x.get("cwd", ""), "minutes": 0.0})
                ss["minutes"] += s["minutes"]
    for a in agg.values():
        a["days"] = sorted(a["days"])
        a["top"] = sorted(a["top"], key=lambda s: -s["minutes"])[:3]
        a["hands_on_share"] = a["hands_on"] / a["known_minutes"] if a["known_minutes"] else None
    return agg


def aggregate_waste(days):
    """Stupidity tax over the window: totals, per site, per day, and the longest binges."""
    tot = {"passive_minutes": 0.0, "active_minutes": 0.0, "estimated_minutes": 0.0, "per_site": {}, "binges": [],
           "per_day": {}}
    for d in days:
        p = C.REPORTS / d / "day.json"
        w = json.loads(p.read_text()).get("waste") if p.exists() else None
        if not w:
            continue
        for k in ("passive_minutes", "active_minutes", "estimated_minutes"):
            tot[k] += w.get(k, 0)
        tot["per_day"][d] = w["passive_minutes"]
        for site, v in w["per_site"].items():
            t = tot["per_site"].setdefault(site, {"passive": 0.0, "active": 0.0, "estimated": 0.0})
            for k in t:
                t[k] += v.get(k, 0)
        tot["binges"] += w["binges"]
    tot["binges"].sort(key=lambda b: -b["minutes"])
    return tot


def waste_panel(waste, days):
    if not waste["per_day"]:
        return ""
    n = len(waste["per_day"])
    avg = waste["passive_minutes"] / n
    peak = max(waste["per_day"].values()) or 1
    bars = "".join(
        f'<div class="daybar"><span>{dt.date.fromisoformat(d):%a %d}</span><div class="meter"><i style="width:{m / peak * 100:.0f}%"></i></div>'
        f'<b>{fmt(m)}</b></div>' for d, m in sorted(waste["per_day"].items()))
    return (C.waste_html(waste, "../../videos/{day}/{hour}.mp4", fmt)
            .replace("</div>", f'<p class="muted">{fmt(avg)} per recorded day · at this rate {avg * 230 / 60:.0f} hours a year '
                               f'(230 working days)</p>{bars}</div>', 1))


def spread(items, n):
    """n items evenly spread over the list, so evidence covers the whole period, not just day one."""
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def previous_minutes(kind, label):
    """Minutes per workflow in the previous period of the same kind, from the history file."""
    if not HISTORY.exists():
        return {}
    with HISTORY.open() as f:
        rows = [r for r in csv.DictReader(f) if r["kind"] == kind and r["period"] < label]
    if not rows:
        return {}
    last = max(r["period"] for r in rows)
    return {r["workflow_id"]: float(r["minutes"]) for r in rows if r["period"] == last}


# ---------- score (Claude) ----------

def score(agg, n_days, wcfg, start, end):
    ccfg = C.load_config()
    who = ccfg["who"]
    about = f" ({ccfg['about'].rstrip('.')})" if ccfg.get("about") else ""
    top = sorted(agg.values(), key=lambda a: -a["minutes"])[:wcfg["topN"]]
    blocks = []
    for a in top:
        sess_lines = []
        a["session_stats"] = []
        for ss in sorted(a["claude_sessions"].values(), key=lambda x: -x["minutes"])[:3]:
            if not Path(ss["path"]).exists():
                continue
            st = {**session_stats(ss["path"], start, end), **{k: ss[k] for k in ("id", "path", "cwd", "minutes")}}
            a["session_stats"].append(st)
            ps = "\n".join(f"      [{t:%a %H:%M}] {x[:400]!r}" for t, x in spread(st["prompts"], wcfg["promptsPerWorkflow"]))
            sess_lines.append(
                f"    session '{st['title'] or '(untitled)'}': {st['turns']} prompts, {st['nudges']} nudges "
                f"(go/continue/yes), Claude worked {st['claude_minutes']} min, Claude then waited on {who} "
                f"{st['waiting_on_user_minutes']} min, {st['ended_on_question']} turns ended on a question, "
                f"effort {st['effort'] or 'unknown'}\n    {who}'s prompts:\n{ps}")
        ho = "unknown" if a["hands_on_share"] is None else f"{a['hands_on_share'] * 100:.0f}% hands-on"
        acts = "\n".join(f"    - {x}" for x in spread(a["activities"], wcfg["activitiesPerWorkflow"]))
        blocks.append(f"""## {a['id']}: {a['name']} ({a['category']})
  {a['minutes']:.0f} min over {len(a['days'])} of {n_days} days, {a['sessions']} sessions, {ho}
  clients: {", ".join(f"{k} {v:.0f}m" for k, v in sorted(a['clients'].items(), key=lambda kv: -kv[1]))}
  observed activities:
{acts}
  linked Claude sessions:
{chr(10).join(sess_lines) or "    none linked"}""")
    rubric = "\n".join(f"- {k}: {v}" for k, v in wcfg["rubric"].items())
    tools = "\n".join(f"- {t}" for t in wcfg["agentTools"])
    guides = load_guides(wcfg)
    prompt = f"""Reference: Anthropic's current prompting guides for the model {who} works with.

{guides}

---

You are reviewing how {who}{about} spent
{n_days} days of recorded work time, to decide which recurring workflows Claude agents should take over.
"Hands-on" = keyboard/mouse input in the last 5 s; the rest is reading, waiting on, or watching a tool.

What an agent can reach today:
{tools}

Workflows (biggest first):

{chr(10).join(blocks)}

Score each workflow 0-5 on:
{rubric}

Then estimate automatable_share (0-1): the share of this workflow's time Claude could remove within a month,
given the tools above. Count waiting/watching time: if {who} babysits a Claude session, removing the
babysitting (better autonomy, notifications, batching) counts as recoverable.
Then name the bottleneck, the reason this workflow still costs {who} time, as exactly one of:
- way_of_working: how he runs it (babysitting a session that could run alone, serial waits, switching between
  many sessions, re-explaining context, manual copying between tools)
- prompt: the prompts themselves fall short of the guides above (no completion condition, missing context or
  sources, vague scope, effort mismatch, asking for things the guides say to drop). Cite the guide section.
- not_handed_off: Claude already does the work end to end and {who} mostly approves or nudges; it should become
  a skill or standing/scheduled agent
- capability_gap: a missing tool, connector or permission, or a task current models cannot do well
- too_little_data: not enough recorded evidence yet
Use the session numbers: many nudges or turns ending on a question point to the prompt or to the unattended-run
guidance; long "Claude waited on {who}" time with short approvals points to not_handed_off; long Claude working
time watched hands-off points to way_of_working.

Ground every claim in the observed activities and {who}'s quoted prompts. Quote 1-3 as evidence. Do not invent
steps you cannot see. If the evidence is too thin to judge, say so and score conservatively.

Return ONLY JSON, no prose:
{{"summary": "<3 sentences: where the time went and the single biggest opportunity>",
  "workflows": [{{"id": "<id>", "scores": {{{", ".join(f'"{k}": 0' for k in wcfg["rubric"])}}},
    "automatable_share": 0.0,
    "agent_shape": "<one of: skill, scheduled agent, hook/automation, new connector, better prompting/autonomy, keep human>",
    "how": "<2 sentences: what Claude would do, concretely>",
    "first_step": "<one action {who} can take this week>",
    "blockers": "<what stops it today, or 'none'>",
    "evidence": ["<quoted activity or prompt>"],
    "bottleneck": "<way_of_working | prompt | not_handed_off | capability_gap | too_little_data>",
    "bottleneck_why": "<2 sentences citing the session numbers or prompts>",
    "prompt_fixes": [{{"issue": "<what the prompt misses>", "guide_section": "<section title from the guides>",
      "rewrite": "<the improved prompt text or standing instruction, ready to paste>"}}]}}]}}"""
    return C.claude(prompt, str(C.ROOT), wcfg["model"], ccfg, tools=False), top


# ---------- outputs ----------

def write_history(kind, label, rows):
    cols = ["kind", "period", "workflow_id", "workflow", "minutes", "hands_on_minutes", "days_seen", "sessions",
            "score", "automatable_share", "recoverable_minutes_per_week", "agent_shape", "bottleneck"]
    old = []
    if HISTORY.exists():
        with HISTORY.open() as f:
            old = [{c: r.get(c, "") for c in cols} for r in csv.DictReader(f)
                   if not (r["kind"] == kind and r["period"] == label)]
    with HISTORY.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(old + rows)


BOTTLENECK = {"way_of_working": "Way of working", "prompt": "The prompt", "not_handed_off": "Not handed off yet",
              "capability_gap": "Capability gap", "too_little_data": "Too little data"}


def fmt(m):
    if m < 1:
        return f"{m * 60:.0f}s"
    return f"{int(m // 60)}h {int(m % 60):02d}m" if m >= 60 else f"{m:.0f}m"


def write_report(kind, label, start, end, n_days, agg, result, top, prev, wcfg, waste=None):
    out_dir = C.REPORTS / kind
    out_dir.mkdir(parents=True, exist_ok=True)
    by_id = {w["id"]: w for w in result.get("workflows", [])}
    weeks = max(n_days / 7, 1 / 7)
    ranked, hist = [], []
    for a in top:
        w = by_id.get(a["id"], {})
        sc = w.get("scores", {})
        score100 = round(sum(sc.get(k, 0) for k in wcfg["rubric"]) / (5 * len(wcfg["rubric"])) * 100)
        share = float(w.get("automatable_share", 0) or 0)
        recover = a["minutes"] * share / weeks
        ranked.append((recover, a, w, sc, score100, share))
        hist.append({"kind": kind, "period": label, "workflow_id": a["id"], "workflow": a["name"],
                     "minutes": round(a["minutes"], 1), "hands_on_minutes": round(a["hands_on"], 1),
                     "days_seen": len(a["days"]), "sessions": a["sessions"], "score": score100,
                     "automatable_share": share, "recoverable_minutes_per_week": round(recover, 1),
                     "agent_shape": w.get("agent_shape", ""), "bottleneck": w.get("bottleneck", "")})
    ranked.sort(key=lambda r: -r[0])
    write_history(kind, label, hist)

    total = sum(a["minutes"] for a in agg.values())
    known = sum(a["known_minutes"] for a in agg.values())
    on = sum(a["hands_on"] for a in agg.values())
    recover_total = sum(r[0] for r in ranked)

    cards = []
    for i, (recover, a, w, sc, s100, share) in enumerate(ranked, 1):
        delta = ""
        if a["id"] in prev:
            d = a["minutes"] - prev[a["id"]]
            delta = f' <span class="muted">({"+" if d >= 0 else "−"}{fmt(abs(d))} vs last)</span>'
        elif prev:
            delta = ' <span class="muted">(new)</span>'
        bars = "".join(
            f'<div class="dim"><span>{html.escape(wcfg["rubric"][k].split(" (")[0])}</span>'
            f'<div class="meter"><i style="width:{sc.get(k, 0) * 20}%"></i></div><b>{sc.get(k, "–")}</b></div>'
            for k in wcfg["rubric"])
        ev = "".join(f"<li>{html.escape(e)}</li>" for e in w.get("evidence", []))
        sessions = []
        for s in a["top"]:
            clips = json.dumps([{"src": f"../../videos/{s['day']}/{c['hour']}.mp4", "t": c["offset"]} for c in s["clips"]])
            thumb = f'<img src="../{s["day"]}/{s["thumbs"][0]}" loading="lazy">' if s.get("thumbs") else ""
            sessions.append(f"""<div class="sess">{thumb}<div><b>{s['day']} {s['start'][11:16]}–{s['end'][11:16]}</b>
<span class="muted">{fmt(s['minutes'])} · {html.escape(s['client'])}</span><br>
<button onclick='play({clips})'>▶ Watch</button> <a href="../{s['day']}/index.html">day report</a></div></div>""")
        ho = "—" if a["hands_on_share"] is None else f"{a['hands_on_share'] * 100:.0f}%"
        bn = w.get("bottleneck", "")
        fixes = "".join(
            f'<div class="fix"><b>{html.escape(fx.get("issue", ""))}</b> <span class="muted">· {html.escape(fx.get("guide_section", ""))}</span>'
            f'<pre>{html.escape(fx.get("rewrite", ""))}</pre></div>' for fx in w.get("prompt_fixes", []) or [])
        sess = "".join(
            f'<div class="sessrow"><b>{html.escape(st["title"] or "(untitled)")}</b> <span class="muted">· {st["turns"]} prompts · '
            f'{st["nudges"]} nudges · Claude worked {st["claude_minutes"]}m · waited on you {st["waiting_on_user_minutes"]}m · '
            f'effort {html.escape(", ".join(f"{k} {v}" for k, v in st["effort"].items()) or "?")}</span><br>'
            f'<code>cd {html.escape(st["cwd"])} && claude --resume {st["id"]}</code><br><code>{html.escape(st["path"])}</code></div>'
            for st in a.get("session_stats", []))
        diag = (f'<div class="diag"><span class="bn bn-{html.escape(bn)}">{html.escape(BOTTLENECK.get(bn, bn or "—"))}</span> '
                f'{html.escape(w.get("bottleneck_why", ""))}{fixes}'
                f'{"<p class=muted>Claude sessions behind this workflow</p>" + sess if sess else ""}</div>')
        cards.append(f"""<details{' open' if i <= 3 else ''}><summary>
<span class="rank">{i}</span>
<div><span class="wf">{html.escape(a['name'])}</span>{delta}<br>
<span class="muted">{html.escape(a['category'])} · {len(a['days'])}/{n_days} days · {a['sessions']} sessions · hands-on {ho}</span></div>
<div class="num"><b>{fmt(a['minutes'])}</b><span class="muted">recorded</span></div>
<div class="num"><b>{s100}</b><span class="muted">score</span></div>
<div class="num hot"><b>{fmt(recover)}</b><span class="muted">recoverable / wk</span></div></summary>
<div class="body">{diag}<div class="cols"><div>
<p><span class="tag">{html.escape(w.get('agent_shape', '—'))}</span> Claude could take ~{share * 100:.0f}%</p>
<p>{html.escape(w.get('how', ''))}</p>
<p><b>First step:</b> {html.escape(w.get('first_step', ''))}</p>
<p><b>Blockers:</b> {html.escape(w.get('blockers', ''))}</p>
<p class="muted">Evidence</p><ul>{ev}</ul></div>
<div>{bars}<p class="muted" style="margin-top:14px">Longest sessions</p>{''.join(sessions)}</div></div></div></details>""")

    title = f"Week {label.split('-W')[1]}" if kind == "weekly" else dt.date.fromisoformat(f"{label}-01").strftime("%B %Y")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WorkTape {html.escape(label)}</title><style>{C.CSS}
summary{{grid-template-columns:28px 1fr 90px 60px 110px}}.rank{{font-size:18px;color:var(--muted)}}
.num{{text-align:right}}.num b{{display:block}}.num span{{font-size:12px}}.hot b{{color:var(--accent)}}
.body{{padding:4px 14px 14px;border-top:1px solid var(--line)}}.cols{{display:grid;grid-template-columns:1fr 1fr;gap:28px}}
.dim{{display:grid;grid-template-columns:1fr 90px 16px;gap:8px;align-items:center;font-size:13px;margin:5px 0}}
.tag{{background:var(--bar);border-radius:4px;padding:1px 7px;font-size:12px;margin-right:6px}}
.sess{{display:flex;gap:10px;margin:8px 0;font-size:13px}}.sess img{{width:110px;border-radius:4px;border:1px solid var(--line)}}
a{{color:var(--accent)}}
.diag{{margin:12px 0 16px;padding:12px;border:1px solid var(--line);border-radius:6px}}
.bn{{font-weight:600;border-radius:4px;padding:2px 8px;margin-right:6px;background:var(--bar)}}
.fix{{margin-top:10px;font-size:13px}}pre{{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:8px;font-size:12px}}
.daybar{{display:grid;grid-template-columns:60px 1fr 60px;gap:8px;align-items:center;font-size:13px;margin:4px 0}}
.sessrow{{font-size:13px;margin:8px 0}}code{{font-size:11px;color:var(--muted);word-break:break-all}}
@media (max-width:640px){{summary{{grid-template-columns:24px 1fr 80px}}summary .num:nth-of-type(2),summary .num:nth-of-type(3){{display:none}}.cols{{grid-template-columns:1fr}}}}
</style></head><body><main>
<p class="muted"><a href="../index.html">← all days</a></p>
<h1>{title}: what Claude could take off your plate</h1>
<p class="muted">{start:%a %d %b} – {end:%a %d %b %Y} · {n_days} days · recorded work time only</p>
<div class="player" id="player"><video id="v" controls playsinline></video>
<div class="bar"><span class="muted">Speed</span><button onclick="sp(1)">1×</button><button onclick="sp(4)">4×</button>
<button onclick="sp(16)">16×</button></div></div>
<div class="stats"><div><b>{fmt(total)}</b><span class="muted">recorded</span></div>
<div><b>{f"{on / known * 100:.0f}%" if known else "—"}</b><span class="muted">hands-on</span></div>
<div><b>{len(agg)}</b><span class="muted">workflows</span></div>
<div><b style="color:var(--accent)">{fmt(recover_total)}</b><span class="muted">recoverable per week (estimate)</span></div></div>
{waste_panel(waste, n_days) if waste else ""}
<p>{html.escape(result.get('summary', ''))}</p>
<p class="muted">Ranked by recoverable time = recorded minutes × share Claude could take, per week. Score = the five rubric
dimensions averaged, 0–100. Estimates come from Claude reading the observations; the evidence under each is what it relied on.</p>
{''.join(cards)}
</main><script>
const v=document.getElementById('v');let rate=4;function sp(r){{rate=r;v.playbackRate=r}}
function play(clips){{const c=clips[0];document.getElementById('player').classList.add('on');
 if(!v.src.endsWith(c.src.replace('../..',''))){{v.src=c.src}}
 const go=()=>{{v.currentTime=c.t;v.playbackRate=rate;v.play()}};
 v.readyState>=1?go():v.addEventListener('loadedmetadata',go,{{once:true}});window.scrollTo({{top:0,behavior:'smooth'}})}}
</script></body></html>"""
    path = out_dir / f"{label}.html"
    path.write_text(page)
    return path


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("weekly", "monthly"):
        print(__doc__)
        sys.exit(1)
    kind = args[0]
    end_arg = args[args.index("--end") + 1] if "--end" in args else None
    start, end, label = window(kind, end_arg, "--mtd" in args)
    days = days_between(start, end)
    wcfg = load_watch_config()
    ensure_daily(days)
    agg = aggregate(days)
    if not agg:
        C.log(f"watcher {kind} {label}: no classified days in {start}..{end}")
        return
    C.log(f"watcher {kind} {label}: {len(agg)} workflows, scoring top {min(len(agg), wcfg['topN'])}")
    waste = aggregate_waste(days)
    result, top = score(agg, len(days), wcfg, start, end)
    path = write_report(kind, label, start, end, len(days), agg, result, top, previous_minutes(kind, label), wcfg, waste)
    C.write_index()
    C.log(f"watcher {kind} {label}: done -> {path}")
    C.notify([f"{kind} review {label}"], verb="Ready:")


if __name__ == "__main__":
    main()
