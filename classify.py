#!/usr/bin/env python3
"""WorkTape daily classifier.

For one day of recordings: sample frames from the hourly videos, have Claude describe
each one, group them into named workflows (reused across days), and write
  ~/WorkTape/reports/<day>/index.html   report with links into the videos
  ~/WorkTape/reports/<day>/day.json     structured output
  ~/WorkTape/data/segments.csv          one row per work segment, all days
  ~/WorkTape/data/workflows.json        the running workflow taxonomy

Usage:
  classify.py                 # yesterday
  classify.py 2026-09-29      # a specific day
  classify.py --catch-up      # every unprocessed day in the last 7 (what launchd runs)
"""

import csv
import datetime as dt
import fcntl
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import shutil

ROOT = Path(os.environ.get("WORKTAPE_ROOT", Path.home() / "WorkTape"))
VIDEOS = ROOT / "videos"
REPORTS = ROOT / "reports"
DATA = ROOT / "data"
CONFIG = ROOT / "classify.json"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

DEFAULTS = {
    "baseEverySeconds": 300,        # broad pass: at least one sample per 5 min of recorded time
    "minGapSeconds": 20,            # busiest possible: one sample per 20 s
    "changeBudget": 40,             # sample again once this much screen change (sum of % per frame) piles up
    "handsOnSeconds": 5,            # input within this many seconds = hands-on, else watching/waiting
    "passiveWeight": 0.25,          # screen change while hands-off (e.g. Claude streaming) counts this much
    "frameSeconds": 2,              # must match WorkTape's intervalSeconds
    "segmentGapMinutes": 5,         # a pause longer than this starts a new segment
    "batchSize": 25,                # frames per vision call
    "visionModel": "sonnet",
    "groupingModel": "opus",
    "name": "",                     # how the prompts refer to you, e.g. "Sam"; empty = "the user"
    "about": "",                    # one sentence on your work, e.g. "runs a data-visualization agency"
    "clients": [],                  # e.g. ["Acme", "Internal", "Personal"]; empty = inferred from the screen
    "categories": ["Research", "Writing", "Data & charts", "Coding & tooling", "Communication",
                   "Meetings", "Review & QA", "Admin & ops", "Other"],
    # Stupidity tax: passive time on these sites (matched against the window title).
    "timeWasters": {
        "X": [" / X", " on X: ", "X (formerly Twitter)"],
        "LinkedIn": ["| LinkedIn", "LinkedIn"],
        "YouTube": ["- YouTube"],
        "Reddit": ["Reddit", "r/"],
        "Instagram": ["Instagram"],
        "Facebook": ["Facebook"],
        "Hacker News": ["Hacker News"],
    },
    # Titles that mean outreach or writing even without typing yet (reading a DM thread, a compose window).
    "activeTitleHints": ["Messaging", "Messages", "Compose", "Draft", "Write article", "Create a post"],
    # Titles that are prospect research, never taxed: LinkedIn profile pages ("Jane Doe | LinkedIn"), Sales Navigator.
    # Matched after stripping a leading "(3) " notification count. Fixed LinkedIn pages are listed so they stay taxed.
    "notTaxed": [
        "^(?!(Feed|Notifications|Search|My Network|Jobs|Messaging|LinkedIn|Home|Post|Posts|Groups|Events|Newsletters|Company|Top Content)\\b)[^|:]+ \\| LinkedIn$",
        "Sales Navigator",
    ],
    "activeKeysPerMinute": 10,      # at least this many keystrokes in the surrounding minute = writing, not scrolling
    "claude": "",                   # empty = found on PATH or in ~/.local/bin
    "ffmpeg": "",                   # empty = found on PATH or in Homebrew
}


def find_tool(name, configured, extra):
    for c in [configured, shutil.which(name), *extra]:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return configured or name


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text()))
    else:
        ROOT.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(DEFAULTS, indent=2))
    cfg["claude"] = find_tool("claude", cfg["claude"], [str(Path.home() / ".local/bin/claude"), "/opt/homebrew/bin/claude",
                                                         "/usr/local/bin/claude"])
    cfg["ffmpeg"] = find_tool("ffmpeg", cfg["ffmpeg"], ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"])
    cfg["who"] = cfg.get("name") or "the user"
    return cfg


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------- 1. frames ----------

def load_hours(day):
    """Each recorded frame of the day: hour, index in that hour's video, timestamp, app, title."""
    frames = []
    for tsv in sorted((VIDEOS / day).glob("*.tsv")):
        hour = tsv.stem
        if not (VIDEOS / day / f"{hour}.mp4").exists():
            continue
        # The video holds one frame per unique capture second, in time order (frames are named HHmmss.jpg),
        # so key by timestamp; duplicate log lines for the same second collapse to one frame.
        by_time = {}
        for line in tsv.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                num = lambda k: float(parts[k]) if len(parts) > k and parts[k].strip() else None  # older logs lack these
                by_time[parts[0]] = (parts[1], parts[2] if len(parts) > 2 else "", num(3), num(4), num(5), num(6))
        for n, t in enumerate(sorted(by_time)):
            app, title, inp, keys, scrolls, clicks = by_time[t]
            frames.append({"hour": hour, "n": n, "time": t, "app": app, "title": title, "input": inp,
                           "keys": keys, "scrolls": scrolls, "clicks": clicks})
    return frames


def measure_change(day, frames, cfg):
    """% of the screen that changed since the previous frame, per frame. Local, no tokens, plain Python:
    decode each hour at 96x60 grayscale and count pixels that moved by more than 16 levels."""
    w, h = 96, 60
    size = w * h
    for hour in sorted({f["hour"] for f in frames}):
        raw = subprocess.run([cfg["ffmpeg"], "-loglevel", "error", "-i", str(VIDEOS / day / f"{hour}.mp4"),
                              "-vf", f"scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                             capture_output=True, check=True).stdout
        imgs = [raw[k:k + size] for k in range(0, len(raw) - size + 1, size)]
        pct = [100.0] + [sum(1 for x, y in zip(a, b) if abs(x - y) > 16) * 100 / size for a, b in zip(imgs, imgs[1:])]
        for f in frames:
            if f["hour"] == hour:
                f["change"] = float(pct[f["n"]]) if f["n"] < len(pct) else 0.0


def fkey(frames, s):
    """Stable id for a frame across re-runs of the same day: '<hour>-<index in that hour>'."""
    return f"{frames[s]['hour']}-{frames[s]['n']:04d}"


def norm_client(name, cfg):
    """Map the model's client label onto the configured list ('Acme' -> 'Acme (internal)')."""
    name = (name or "Unknown").strip()
    for c in cfg["clients"]:
        base = c.split(" (")[0].lower()
        if name.lower() == c.lower() or name.lower() == base or any(part.strip().lower() == name.lower() for part in c.split("/")):
            return c
    return name


def hands_on(f, cfg):
    return None if f["input"] is None else f["input"] <= cfg["handsOnSeconds"]


def choose_samples(frames, cfg):
    """Broad first, denser where things move:
    - one sample per baseEverySeconds regardless (the wide net)
    - one when the app or window title changes
    - one whenever accumulated screen change passes changeBudget, at most one per minGapSeconds.
      Change while hands-off (Claude streaming, a video) counts at passiveWeight, so waiting stays cheap."""
    base = max(1, int(cfg["baseEverySeconds"] / cfg["frameSeconds"]))
    gap = max(1, int(cfg["minGapSeconds"] / cfg["frameSeconds"]))
    samples, last, acc = [], None, 0.0
    for idx, f in enumerate(frames):
        if last is not None:
            acc += f.get("change", 0) * (cfg["passiveWeight"] if hands_on(f, cfg) is False else 1)
        prev = frames[last] if last is not None else None
        switched = prev is not None and (f["app"], f["title"]) != (prev["app"], prev["title"])
        if (prev is None or f["hour"] != prev["hour"] or idx - last >= base
                or (switched and idx - last >= 5) or (acc >= cfg["changeBudget"] and idx - last >= gap)):
            samples.append(idx)
            last, acc = idx, 0.0
    return samples


def extract(day, frames, samples, out_dir, cfg):
    out_dir.mkdir(parents=True, exist_ok=True)
    by_hour = {}
    for s in samples:
        by_hour.setdefault(frames[s]["hour"], []).append(s)
    for hour, idxs in by_hour.items():
        wanted = [idx for idx in idxs if not (out_dir / f"{hour}-{frames[idx]['n']:04d}.jpg").exists()]
        if not wanted:
            continue
        expr = "+".join(f"eq(n\\,{frames[i]['n']})" for i in wanted)
        tmp = out_dir / f"_tmp_{hour}"
        tmp.mkdir(exist_ok=True)
        subprocess.run([cfg["ffmpeg"], "-y", "-loglevel", "error", "-i", str(VIDEOS / day / f"{hour}.mp4"),
                        "-vf", f"select='{expr}',scale=1456:-2", "-fps_mode", "passthrough",
                        "-q:v", "4", str(tmp / "%04d.jpg")], check=True)
        for k, idx in enumerate(wanted, start=1):
            src = tmp / f"{k:04d}.jpg"
            if src.exists():
                src.rename(out_dir / f"{hour}-{frames[idx]['n']:04d}.jpg")
        for leftover in tmp.iterdir():
            leftover.unlink()
        tmp.rmdir()


# ---------- 2. Claude ----------

def claude(prompt, cwd, model, cfg, tools=True):
    cmd = [cfg["claude"], "-p", prompt, "--model", model, "--output-format", "text"]
    if tools:
        cmd += ["--allowedTools", "Read"]
    for attempt in range(2):
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=1800)
        m = re.search(r"(\{.*\}|\[.*\])", r.stdout, re.S)
        if r.returncode == 0 and m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        log(f"  claude call failed (attempt {attempt + 1}): {r.stderr[-300:] or r.stdout[-300:]}")
    raise RuntimeError("claude returned no parseable JSON")


def describe(day, frames, samples, frames_dir, cfg, cache):
    """Pass 1: one specific sentence per sampled frame. Cached in observations.json."""
    todo = [s for s in samples if fkey(frames, s) not in cache]
    for b in range(0, len(todo), cfg["batchSize"]):
        batch = todo[b:b + cfg["batchSize"]]
        log(f"  describing frames {b + 1}-{b + len(batch)} of {len(todo)}")
        def ctx(s):
            ho = hands_on(frames[s], cfg)
            return "unknown" if ho is None else ("typing/clicking" if ho else f"no input for {int(frames[s]['input'])}s")
        rows = "\n".join(
            f"{s} | {frames[s]['time']} | {frames[s]['app']} | {frames[s]['title']} | {ctx(s)} | "
            f"{frames[s]['hour']}-{frames[s]['n']:04d}.jpg"
            for s in batch)
        who = cfg["who"]
        about = f"{who} {cfg['about'].rstrip('.')}. " if cfg.get("about") else ""
        known = (f"Known clients: {', '.join(cfg['clients'])}." if cfg["clients"]
                 else "No client list is configured; name the client or project from what is on screen.")
        prompt = f"""These are screenshots of {who}'s screen during work on {day}, sampled more densely where the screen changes.
{about}{known}

Read every image file listed below (id | time | app | window title | keyboard/mouse input | file) with the Read tool.

{rows}

For each one, return what {who} is doing. Be specific: name the document, repo, chat, person, chart or
client visible on screen. Describe the task, not the app ("reviewing Claude's rebuild of the Q3 revenue chart",
not "using Claude"). If he has no input and is watching Claude or another tool work, say he is waiting and on what.

Return ONLY a JSON array, one object per id, no prose:
[{{"id": <id>, "activity": "<one specific sentence>", "client": "<exactly one known client as written above, 'Other: <name>', or 'Unknown'>",
  "task": "<short verb phrase, e.g. 'draft outreach email'>", "tools": ["<apps/tools in use>"],
  "chat_title": "<exact title of the Claude chat or Claude Code session in focus (header or highlighted in the sidebar), else ''>"}}]"""
        for row in claude(prompt, frames_dir, cfg["visionModel"], cfg):
            if 0 <= int(row["id"]) < len(frames):
                row["client"] = norm_client(row.get("client"), cfg)
                cache[fkey(frames, int(row["id"]))] = row
    return cache


def group(day, frames, samples, obs, taxonomy, cfg):
    """Pass 2: assign each observation to a workflow, reusing the taxonomy where it fits."""
    weight = sample_weights(frames, samples)
    lines = "\n".join(
        f"{s} | {frames[s]['time'][11:16]} | {weight[s] * cfg['frameSeconds'] / 60:.1f} min | "
        f"{obs[fkey(frames, s)].get('client', '')} | {obs[fkey(frames, s)].get('task', '')} | "
        f"{obs[fkey(frames, s)].get('activity', '')}"
        for s in samples if fkey(frames, s) in obs)
    existing = json.dumps({k: {"name": v["name"], "category": v["category"], "description": v["description"]}
                           for k, v in taxonomy.items()}, indent=1)
    who = cfg["who"]
    prompt = f"""You are building a taxonomy of the recurring workflows {who} spends work time on, so they can later
hand the biggest ones to AI agents. Below are minute-by-minute observations of his screen on {day}
(id | time | minutes represented | client | task | activity).

{lines}

Existing workflows from previous days (reuse these ids whenever one fits):
{existing}

Assign every observation to exactly one workflow. A workflow is a repeatable unit of work, named
verb + object, independent of client (the client is tracked separately). It should be specific enough that
an agent could be briefed on it ("Rebuild a client chart from source data", "Review and steer a Claude Code
session", "Triage Slack and email") and general enough to recur across days. Consecutive observations of
one task belong to the same workflow. Create a new workflow only when no existing one fits.
Categories: {", ".join(cfg["categories"])}.

List only NEW workflows under "workflows". Return ONLY JSON, no prose:
{{"workflows": [{{"id": "<kebab-case>", "name": "<verb + object>", "category": "<one category>",
   "description": "<one sentence: what this workflow consists of>"}}],
  "assignments": {{"<observation id>": "<workflow id>", ...}}}}"""
    return claude(prompt, str(ROOT), cfg["groupingModel"], cfg, tools=False)


def sample_weights(frames, samples):
    """How many recorded frames each sample stands for (itself plus following frames up to the next sample)."""
    weight = {}
    bounds = samples + [len(frames)]
    for a, b in zip(bounds, bounds[1:]):
        weight[a] = b - a
    return weight


# ---------- 2a. stupidity tax (local, no tokens) ----------

def site_of(title, cfg):
    for site, pats in cfg["timeWasters"].items():
        if any(p.lower() in title.lower() for p in pats):
            return site
    return None


def compute_waste(frames, cfg):
    """Time on time-waster sites, split into passive (scrolling/reading) and active (typing a post, comment or DM).
    Active = enough keystrokes in the minute around the frame, or a messaging/compose window."""
    fs = cfg["frameSeconds"]
    ts = [dt.datetime.strptime(f["time"], "%Y-%m-%d %H:%M:%S") for f in frames]
    half = dt.timedelta(seconds=30)
    lo = hi = 0
    keys_win = 0.0
    state = []   # per frame: None (not a waste site) or (site, "passive" | "active" | "unknown")
    for i, f in enumerate(frames):
        while hi < len(frames) and ts[hi] <= ts[i] + half:
            keys_win += frames[hi]["keys"] or 0
            hi += 1
        while ts[lo] < ts[i] - half:
            keys_win -= frames[lo]["keys"] or 0
            lo += 1
        site = site_of(f["title"], cfg)
        bare = re.sub(r"^\(\d+\)\s*", "", f["title"]).strip()
        if not site:
            state.append(None)
        elif (any(h.lower() in f["title"].lower() for h in cfg["activeTitleHints"])
              or any(re.search(rx, bare) for rx in cfg["notTaxed"])
              or keys_win >= cfg["activeKeysPerMinute"]):
            state.append((site, "active"))
        elif f["keys"] is None:
            state.append((site, "estimated"))   # before keystroke logging: counted as passive, flagged
        else:
            state.append((site, "passive"))

    per_site, binges, cur = {}, [], None
    for i, st in enumerate(state):
        if st:
            d = per_site.setdefault(st[0], {"passive": 0.0, "active": 0.0, "estimated": 0.0})
            d["active" if st[1] == "active" else "passive"] += fs / 60
            if st[1] == "estimated":
                d["estimated"] += fs / 60
        passive = bool(st) and st[1] in ("passive", "estimated")
        if passive and cur and cur["site"] == st[0] and (ts[i] - cur["_last"]).total_seconds() <= 60:
            cur["frames"] += 1
            cur["_last"] = ts[i]
        elif passive:
            cur = {"site": st[0], "start": frames[i]["time"], "_last": ts[i], "frames": 1,
                   "clip": {"hour": frames[i]["hour"], "offset": frames[i]["n"] * fs}}
            binges.append(cur)
        else:
            cur = None
    for b in binges:
        b["end"] = b.pop("_last").strftime("%Y-%m-%d %H:%M:%S")
        b["minutes"] = round(b.pop("frames") * fs / 60, 1)
    binges = [b for b in binges if b["minutes"] >= 0.5]
    for d in per_site.values():
        for k in d:
            d[k] = round(d[k], 1)
    return {"passive_minutes": round(sum(d["passive"] for d in per_site.values()), 1),
            "active_minutes": round(sum(d["active"] for d in per_site.values()), 1),
            "estimated_minutes": round(sum(d["estimated"] for d in per_site.values()), 1),
            "per_site": per_site, "binges": sorted(binges, key=lambda b: -b["minutes"])}


def write_waste_csv(day, waste):
    path = DATA / "waste.csv"
    cols = ["date", "site", "passive_minutes", "active_minutes", "estimated_minutes"]
    rows = []
    if path.exists():
        with path.open() as f:
            rows = [r for r in csv.DictReader(f) if r["date"] != day]
    for site, d in waste["per_site"].items():
        rows.append({"date": day, "site": site, "passive_minutes": d["passive"], "active_minutes": d["active"],
                     "estimated_minutes": d["estimated"]})
    rows.sort(key=lambda r: (r["date"], r["site"]))
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def waste_html(waste, video_prefix, fmt):
    """The stupidity-tax panel, shared by the daily report and the watchers."""
    if not waste["per_site"]:
        return '<div class="tax"><b>Stupidity tax: 0m.</b> <span class="muted">No time on X, LinkedIn or the other listed sites.</span></div>'
    sites = " · ".join(f"{html.escape(s)} {fmt(d['passive'])}" for s, d in
                       sorted(waste["per_site"].items(), key=lambda kv: -kv[1]["passive"]) if d["passive"])
    fine = f"{fmt(waste['active_minutes'])} of it was writing, outreach or profile research (not taxed)" if waste["active_minutes"] else ""
    unk = (f"includes {fmt(waste['estimated_minutes'])} estimated: recorded before keystroke logging, counted as passive"
           if waste.get("estimated_minutes") else "")
    rows = "".join(
        f'<li>{b["start"][5:16]}–{b["end"][11:16]} · {html.escape(b["site"])} · <b>{fmt(b["minutes"])}</b> '
        f'<button onclick=\'play({json.dumps([{"src": video_prefix.format(day=b["start"][:10], hour=b["clip"]["hour"]), "t": b["clip"]["offset"]}])})\'>▶ Watch</button></li>'
        for b in waste["binges"][:8])
    return f"""<div class="tax"><div class="taxhead"><b>Stupidity tax: {fmt(waste['passive_minutes'])}</b>
<span class="muted">passive scrolling on {sites or '—'}</span></div>
<p class="muted">{" · ".join(x for x in (fine, unk) if x)}</p>
{'<p class="muted">Longest binges</p><ul>' + rows + '</ul>' if rows else ''}</div>"""


# ---------- 2b. Claude Code sessions ----------

def _local(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


def _text(message):
    c = (message or {}).get("content", "")
    if isinstance(c, list):
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return re.sub(r"<([a-z-]+)>.*?</\1>", "", c or "", flags=re.S).strip()


def load_sessions(day):
    """Claude Code / Code-tab sessions with activity on this day: title, transcript path, message times."""
    start = dt.datetime.fromisoformat(day)
    end = start + dt.timedelta(days=1)
    sessions = []
    for path in CLAUDE_PROJECTS.glob("*/*.jsonl"):
        if dt.datetime.fromtimestamp(path.stat().st_mtime) < start:
            continue
        title, cwd, events, first, entry = "", "", [], "", ""
        with path.open(errors="ignore") as f:
            for line in f:
                try:
                    j = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if j.get("type") == "custom-title":
                    title = j.get("customTitle", title)
                if j.get("type") not in ("user", "assistant") or "timestamp" not in j:
                    continue
                t = _local(j["timestamp"])
                if not start <= t < end:
                    continue
                cwd = j.get("cwd", cwd)
                entry = j.get("entrypoint", entry)
                human = (j["type"] == "user" and "toolUseResult" not in j and not j.get("isSidechain")
                         and (j.get("origin") or {}).get("kind") == "human")
                if human:
                    txt = _text(j.get("message"))
                    if not txt:
                        human = False
                    elif not first:
                        first = txt[:300]
                events.append((t, human))
        # skip headless runs (WorkTape's own classifier calls, scripts): only sessions the user sits in
        if events and entry in INTERACTIVE and not cwd.startswith(str(ROOT)):
            sessions.append({"id": path.stem, "title": title, "path": str(path), "cwd": cwd,
                             "events": events, "first_prompt": first})
    return sessions


INTERACTIVE = {"claude-desktop", "cli"}
CLAUDE_APPS = {"com.anthropic.claudefordesktop", "com.apple.Terminal", "com.googlecode.iterm2", "com.mitchellh.ghostty",
               "dev.warp.Warp-Stable"}
QUOTED = re.compile(r"['\u2018\u201c\"]([^'\u2019\u201d\"]{6,80})['\u2019\u201d\"]")


def link_sessions(segments, sessions, obs, frames):
    """Attach the Claude sessions a segment was spent in: a visible chat title that matches, or a session
    the user typed into during the segment. Sessions that only ran on their own don't count without a title match."""
    pad = dt.timedelta(seconds=30)
    for seg in segments:
        a = dt.datetime.fromisoformat(seg["start"]) - pad
        b = dt.datetime.fromisoformat(seg["end"]) + pad
        # Only frames where Claude or a terminal was in front can tell us which session he was in.
        claude_samples = [sm for sm in seg["samples"] if frames[sm]["app"] in CLAUDE_APPS]
        if not claude_samples:
            seg["sessions"] = []
            continue
        seen = set()
        for sm in claude_samples:
            o = obs.get(fkey(frames, sm), {})
            if "chat_title" in o:            # the chat in focus, read off the screen
                if o["chat_title"]:
                    seen.add(o["chat_title"].strip().lower())
            else:                            # older descriptions: fall back to quoted names
                seen.update(q.strip().lower() for q in QUOTED.findall(o.get("activity", "")))
        found = []
        for s in sessions:
            t = s["title"].lower()
            title_hit = bool(t) and any(len(x) >= 6 and (x in t or t in x) for x in seen)
            human = sum(1 for ts, h in s["events"] if h and a <= ts <= b)
            if title_hit or human:
                found.append({"id": s["id"], "title": s["title"] or "(untitled)", "path": s["path"], "cwd": s["cwd"],
                              "first_prompt": s["first_prompt"], "human_turns": human, "title_match": title_hit,
                              "_rank": title_hit * 100 + human})
        if any(x["title_match"] for x in found):      # a title on screen beats timing
            found = [x for x in found if x["title_match"]]
        found.sort(key=lambda x: -x["_rank"])
        for x in found:
            x.pop("_rank")
        seg["sessions"] = found[:3]


def write_sessions_csv(day, segments):
    path = DATA / "sessions.csv"
    cols = ["date", "segment_start", "segment_end", "workflow_id", "workflow", "client", "session_id",
            "session_title", "human_turns", "title_match", "transcript", "cwd"]
    rows = []
    if path.exists():
        with path.open() as f:
            rows = [r for r in csv.DictReader(f) if r["date"] != day]
    for seg in segments:
        for s in seg.get("sessions", []):
            rows.append({"date": day, "segment_start": seg["start"], "segment_end": seg["end"],
                         "workflow_id": seg["workflow"], "workflow": seg["workflow_name"], "client": seg["client"],
                         "session_id": s["id"], "session_title": s["title"], "human_turns": s["human_turns"],
                         "title_match": s["title_match"], "transcript": s["path"], "cwd": s["cwd"]})
    rows.sort(key=lambda r: r["segment_start"])
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


# ---------- 3. segments + outputs ----------

def build_segments(day, frames, samples, obs, assign, taxonomy, cfg):
    bounds = samples + [len(frames)]
    per_frame = []   # (frame index, workflow id, client, sample index)
    for a, b in zip(bounds, bounds[1:]):
        wf = assign.get(str(a), "unclassified")
        client = obs.get(fkey(frames, a), {}).get("client", "Unknown")
        per_frame += [(i, wf, client, a) for i in range(a, b)]

    gap = dt.timedelta(minutes=cfg["segmentGapMinutes"])
    ts = lambda i: dt.datetime.strptime(frames[i]["time"], "%Y-%m-%d %H:%M:%S")
    segments, cur = [], None
    for i, wf, client, s in per_frame:
        f = frames[i]
        if not (cur and cur["workflow"] == wf and cur["client"] == client and ts(i) - cur["_last"] <= gap):
            cur = {"workflow": wf, "client": client, "start": f["time"], "frames": 0, "hands_on_frames": 0,
                   "known_input_frames": 0, "_change": 0.0, "samples": [],
                   "clips": [{"hour": f["hour"], "offset": f["n"] * cfg["frameSeconds"]}]}
            segments.append(cur)
        cur["_last"] = ts(i)
        cur["frames"] += 1
        cur["_change"] += f.get("change", 0)
        if hands_on(f, cfg) is not None:
            cur["known_input_frames"] += 1
            cur["hands_on_frames"] += hands_on(f, cfg)
        if s not in cur["samples"]:
            cur["samples"].append(s)
        if f["hour"] != cur["clips"][-1]["hour"]:
            cur["clips"].append({"hour": f["hour"], "offset": f["n"] * cfg["frameSeconds"]})

    for seg in segments:
        seg["end"] = seg.pop("_last").strftime("%Y-%m-%d %H:%M:%S")
        seg["minutes"] = round(seg["frames"] * cfg["frameSeconds"] / 60, 1)
        # hands-on share among frames that have input data (None for recordings made before input logging)
        seg["hands_on_share"] = (round(seg["hands_on_frames"] / seg["known_input_frames"], 2)
                                 if seg["known_input_frames"] else None)
        seg["screen_change"] = round(seg.pop("_change") / seg["frames"], 1)   # avg % of screen changing per frame
        seg["workflow_name"] = taxonomy.get(seg["workflow"], {}).get("name", seg["workflow"])
        seg["category"] = taxonomy.get(seg["workflow"], {}).get("category", "Other")
        acts = []
        for s in seg["samples"]:
            a = obs.get(fkey(frames, s), {}).get("activity", "")
            if a and a not in acts:
                acts.append(a)
        seg["activities"] = acts
        seg["thumbs"] = [f"frames/{frames[s]['hour']}-{frames[s]['n']:04d}.jpg" for s in seg["samples"]]
    return segments


def write_csv(day, segments):
    path = DATA / "segments.csv"
    cols = ["date", "workflow_id", "workflow", "category", "client", "start", "end", "active_minutes",
            "hands_on_minutes", "screen_change_pct", "video", "offset_seconds", "summary"]
    rows = []
    if path.exists():
        with path.open() as f:
            rows = [{c: r.get(c, "") for c in cols} for r in csv.DictReader(f) if r["date"] != day]
    for s in segments:
        rows.append({"date": day, "workflow_id": s["workflow"], "workflow": s["workflow_name"],
                     "category": s["category"], "client": s["client"], "start": s["start"], "end": s["end"],
                     "active_minutes": s["minutes"],
                     "hands_on_minutes": "" if s["hands_on_share"] is None else round(s["minutes"] * s["hands_on_share"], 1),
                     "screen_change_pct": s["screen_change"], "video": str(VIDEOS / day / f"{s['clips'][0]['hour']}.mp4"),
                     "offset_seconds": s["clips"][0]["offset"], "summary": " / ".join(s["activities"][:3])})
    rows.sort(key=lambda r: r["start"])
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def update_taxonomy(taxonomy, new, segments, day):
    for w in new:
        if w["id"] not in taxonomy:
            taxonomy[w["id"]] = {**w, "first_seen": day}
    for s in segments:
        t = taxonomy.setdefault(s["workflow"], {"id": s["workflow"], "name": s["workflow"], "category": "Other",
                                                "description": "", "first_seen": day})
        t["last_seen"] = max(t.get("last_seen", day), day)
    (DATA / "workflows.json").write_text(json.dumps(taxonomy, indent=2))


# ---------- 4. report ----------

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a66;--line:#e4e1da;--card:#fff;--accent:#c2410c;--bar:#e8d8c8}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--muted:#9b9992;--line:#2d2c29;--card:#1e1d1b;--accent:#fb923c;--bar:#4a3526}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 80px}h1{font-size:24px;margin:0 0 4px}.muted{color:var(--muted)}
.player{position:sticky;top:0;z-index:2;background:var(--bg);padding:12px 0;border-bottom:1px solid var(--line);display:none}
.player.on{display:block}video{width:100%;max-height:55vh;background:#000;border-radius:6px}
.player .bar{display:flex;gap:8px;align-items:center;margin-top:6px;font-size:13px}
button{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:5px;padding:3px 9px;cursor:pointer}
button:hover{border-color:var(--accent)}
.stats{display:flex;gap:28px;margin:18px 0 24px;flex-wrap:wrap}.stats b{display:block;font-size:22px}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0}
summary{cursor:pointer;padding:12px 14px;display:grid;grid-template-columns:1fr 90px 160px;gap:12px;align-items:center;list-style:none}
summary::-webkit-details-marker{display:none}
.wf{font-weight:600}.cat{font-size:12px;color:var(--muted);font-weight:400;margin-left:6px}
.meter{height:6px;background:var(--line);border-radius:3px;overflow:hidden}.meter i{display:block;height:100%;background:var(--accent)}
.seg{display:grid;grid-template-columns:150px 1fr 150px;gap:14px;padding:12px 14px;border-top:1px solid var(--line);align-items:start}
.seg img{width:150px;border-radius:4px;border:1px solid var(--line);cursor:zoom-in}
.tax{border:1px solid var(--accent);border-radius:8px;padding:12px 14px;margin:0 0 20px}
.tax ul{margin:4px 0 0;padding-left:18px;font-size:13px}.tax li{margin:3px 0}.taxhead b{font-size:18px;margin-right:8px}
.seg ul{margin:4px 0 0;padding-left:18px;color:var(--muted);font-size:13px}
ul.sess{color:var(--fg)}code{font-size:11px;color:var(--muted);word-break:break-all}.client{font-size:12px;color:var(--accent)}
@media (max-width:640px){summary{grid-template-columns:1fr 70px}.meterwrap{display:none}.seg{grid-template-columns:1fr}.seg img{width:100%}}
"""


def write_report(day, segments, out_dir, waste=None):
    total = sum(s["minutes"] for s in segments) or 1
    by_wf = {}
    for s in segments:
        by_wf.setdefault(s["workflow"], []).append(s)
    order = sorted(by_wf.items(), key=lambda kv: -sum(s["minutes"] for s in kv[1]))
    by_client = {}
    for s in segments:
        by_client[s["client"]] = by_client.get(s["client"], 0) + s["minutes"]

    def fmt(m):
        if m < 1:
            return f"{m * 60:.0f}s"
        return f"{int(m // 60)}h {int(m % 60):02d}m" if m >= 60 else f"{m:.0f}m"

    def hands(segs):
        known = [s for s in segs if s["hands_on_share"] is not None]
        if not known:
            return ""
        on = sum(s["minutes"] * s["hands_on_share"] for s in known)
        return f"{fmt(on)} hands-on · {fmt(sum(s['minutes'] for s in known) - on)} waiting/watching"

    def sess_html(seg):
        if not seg.get("sessions"):
            return ""
        items = "".join(
            f'<li><b>{html.escape(x["title"])}</b> <span class="muted">· {x["human_turns"]} of your messages'
            f'{" · title on screen" if x["title_match"] else ""}</span><br><code>{html.escape(x["path"])}</code></li>'
            for x in seg["sessions"])
        return f'<p class="muted" style="margin:8px 0 2px">Claude sessions</p><ul class="sess">{items}</ul>'

    parts = []
    for wf, segs in order:
        mins = sum(s["minutes"] for s in segs)
        clients = sorted({s["client"] for s in segs})
        rows = []
        for s in sorted(segs, key=lambda s: s["start"]):
            clips = json.dumps([{"src": f"../../videos/{day}/{c['hour']}.mp4", "t": c["offset"]} for c in s["clips"]])
            acts = "".join(f"<li>{html.escape(a)}</li>" for a in s["activities"][:5])
            thumb = f'<img src="{s["thumbs"][0]}" loading="lazy" onclick="window.open(this.src)">' if s["thumbs"] else ""
            rows.append(f"""<div class="seg"><div><b>{s['start'][11:16]}–{s['end'][11:16]}</b><br>
<span class="muted">{fmt(s['minutes'])} recorded</span><br>
<span class="muted">{hands([s])}</span><br><button onclick='play({clips})'>▶ Watch</button></div>
<div><span class="client">{html.escape(s['client'])}</span><ul>{acts}</ul>{sess_html(s)}</div>{thumb}</div>""")
        parts.append(f"""<details><summary><div><span class="wf">{html.escape(segs[0]['workflow_name'])}</span>
<span class="cat">{html.escape(segs[0]['category'])} · {html.escape(', '.join(clients))} · {len(segs)} segment{'s' if len(segs) > 1 else ''}
{(' · ' + hands(segs)) if hands(segs) else ''}</span></div>
<div><b>{fmt(mins)}</b> <span class="muted">{mins / total * 100:.0f}%</span></div>
<div class="meterwrap"><div class="meter"><i style="width:{mins / total * 100:.1f}%"></i></div></div></summary>{''.join(rows)}</details>""")

    clients_line = " · ".join(f"{html.escape(c)} {fmt(m)}" for c, m in sorted(by_client.items(), key=lambda kv: -kv[1]))
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WorkTape {day}</title><style>{CSS}</style></head><body><main>
<p class="muted"><a href="../index.html" style="color:inherit">← all days</a></p>
<h1>Where the work went · {dt.date.fromisoformat(day):%a %d %b %Y}</h1>
<p class="muted">Recorded time only (the apps and browsers in ~/WorkTape/config.json). Idle and locked time excluded.
"Hands-on" means keyboard or mouse input in the last 5 seconds; the rest is reading, waiting or watching a tool work.</p>
<div class="player" id="player"><video id="v" controls playsinline></video>
<div class="bar"><span class="muted">Speed</span><button onclick="sp(1)">1×</button><button onclick="sp(4)">4×</button>
<button onclick="sp(16)">16×</button><span id="clipinfo" class="muted"></span></div></div>
<div class="stats"><div><b>{fmt(total)}</b><span class="muted">recorded</span></div>
<div><b>{hands(segments).split(' · ')[0] or '—'}</b><span class="muted">of it with your hands on</span></div>
<div><b>{len(order)}</b><span class="muted">workflows</span></div><div><b>{len(segments)}</b><span class="muted">segments</span></div></div>
<p class="muted">{clients_line}</p>
{waste_html(waste, "../../videos/{day}/{hour}.mp4", fmt) if waste else ""}
{''.join(parts)}
</main><script>
const v=document.getElementById('v');let rate=4;
function sp(r){{rate=r;v.playbackRate=r}}
function play(clips){{const c=clips[0];document.getElementById('player').classList.add('on');
 document.getElementById('clipinfo').textContent=clips.length>1?`(continues in ${{clips.length-1}} more hour file${{clips.length>2?'s':''}})`:'';
 if(!v.src.endsWith(c.src.replace('../..',''))){{v.src=c.src}}
 const go=()=>{{v.currentTime=c.t;v.playbackRate=rate;v.play()}};
 v.readyState>=1?go():v.addEventListener('loadedmetadata',go,{{once:true}});window.scrollTo({{top:0,behavior:'smooth'}})}}
</script></body></html>"""
    (out_dir / "index.html").write_text(page)


def write_index():
    days = sorted((p.name for p in REPORTS.iterdir() if (p / "day.json").exists()), reverse=True)
    rows = []
    for d in days:
        j = json.loads((REPORTS / d / "day.json").read_text())
        mins = sum(s["minutes"] for s in j["segments"])
        top = {}
        for s in j["segments"]:
            top[s["workflow_name"]] = top.get(s["workflow_name"], 0) + s["minutes"]
        top3 = ", ".join(k for k, _ in sorted(top.items(), key=lambda kv: -kv[1])[:3])
        tax = (j.get("waste") or {}).get("passive_minutes")
        rows.append(f'<tr><td><a href="{d}/index.html">{dt.date.fromisoformat(d):%a %d %b}</a></td><td>{mins / 60:.1f}h</td>'
                    f'<td>{"—" if tax is None else f"{tax:.0f}m"}</td><td>{html.escape(top3)}</td></tr>')
    links = []
    for kind in ("monthly", "weekly"):
        for p in sorted((REPORTS / kind).glob("*.html"), reverse=True)[:6]:
            links.append(f'<a href="{kind}/{p.name}">{kind.title()} review {p.stem}</a>')
    reviews = f"<p>{' · '.join(links)}</p>" if links else ""
    lv = DATA / "live.json"
    if lv.exists():
        L = json.loads(lv.read_text())
        if L["day"] == dt.date.today().isoformat():
            fm = lambda m: f"{int(m // 60)}h {int(m % 60):02d}m" if m >= 60 else f"{m:.0f}m"
            apps = " · ".join(f"{html.escape(k)} {fm(v)}" for k, v in L["by_app"].items())
            sites = " · ".join(f"{html.escape(k)} {fm(v)}" for k, v in L["tax_sites"].items())
            reviews = f"""<div class="tax"><div class="taxhead"><b>Today so far: {fm(L['minutes'])} recorded</b>
<span class="muted">updated {L['updated']} · the full workflow report arrives at 07:00 tomorrow</span></div>
<p>Stupidity tax: <b>{fm(L['tax_minutes'])}</b>{(' <span class=muted>(' + sites + ')</span>') if sites else ''}
{f" · hands-on {fm(L['hands_on_minutes'])}" if L['hands_on_minutes'] else ''}</p>
<p class="muted">{apps}</p></div>""" + reviews
    (REPORTS / "index.html").write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>WorkTape days</title>
<style>{CSS} #bar{{display:none;align-items:center;gap:12px;margin:0 0 16px}}#bar button{{font-size:14px;padding:6px 14px}}
table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;padding:8px;border-bottom:1px solid var(--line)}}a{{color:var(--accent)}}</style>
</head><body><main><h1>WorkTape</h1><p class="muted">One report per day. Raw data: ~/WorkTape/data/segments.csv</p>
<div id="bar"><button id="run" onclick="classifyNow()">Classify now</button><span id="st" class="muted">Classifies every day not yet done, plus today so far.</span></div>
{reviews}<table><tr><th>Day</th><th>Recorded</th><th>Stupidity tax</th><th>Top workflows</th></tr>{''.join(rows)}</table></main>
<script>
const h = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.worktape;
if (h) {{ document.getElementById('bar').style.display = 'flex'; h.postMessage('hello'); }}
function classifyNow() {{ h.postMessage('classify'); }}
window.worktapeStatus = (t, busy) => {{
  document.getElementById('run').disabled = !!busy;
  document.getElementById('st').textContent = t || 'Classifies every day not yet done, plus today so far.';
}};
</script></body></html>""")


# ---------- live "today so far" (local, no tokens) ----------

def load_today(day):
    """Every frame recorded today: finished hours (videos/*.tsv) plus the hour in progress (frames/*/log.tsv)."""
    by_time = {}
    logs = sorted((VIDEOS / day).glob("*.tsv")) + sorted((ROOT / "frames" / day).glob("*/log.tsv"))
    for tsv in logs:
        for line in tsv.read_text(errors="ignore").splitlines():
            p = line.split("\t")
            if len(p) >= 2:
                num = lambda k: float(p[k]) if len(p) > k and p[k].strip() else None
                by_time[p[0]] = {"time": p[0], "hour": p[0][11:13], "n": 0, "app": p[1],
                                 "title": p[2] if len(p) > 2 else "", "input": num(3), "keys": num(4)}
    return [by_time[t] for t in sorted(by_time)]


APP_NAMES = {"com.anthropic.claudefordesktop": "Claude", "com.brave.Browser": "Brave", "com.google.Chrome": "Chrome",
             "company.thebrowser.Browser": "Arc", "com.apple.Safari": "Safari", "com.microsoft.edgemac": "Edge",
             "com.apple.Terminal": "Terminal", "com.googlecode.iterm2": "iTerm", "com.mitchellh.ghostty": "Ghostty"}


def live(cfg):
    day = dt.date.today().isoformat()
    frames = load_today(day)
    fs = cfg["frameSeconds"]
    waste = compute_waste(frames, cfg)
    per = {}
    for f in frames:
        name = site_of(f["title"], cfg) or APP_NAMES.get(f["app"], f["app"].split(".")[-1])
        per[name] = per.get(name, 0) + fs / 60
    hands = [f for f in frames if f["input"] is not None]
    on = sum(1 for f in hands if f["input"] <= cfg["handsOnSeconds"]) * fs / 60
    (DATA / "live.json").write_text(json.dumps({
        "day": day, "updated": dt.datetime.now().strftime("%H:%M"), "minutes": round(len(frames) * fs / 60, 1),
        "hands_on_minutes": round(on, 1), "tax_minutes": waste["passive_minutes"],
        "tax_sites": {k: v["passive"] for k, v in waste["per_site"].items() if v["passive"]},
        "by_app": dict(sorted(((k, round(v, 1)) for k, v in per.items()), key=lambda kv: -kv[1])[:6])}))
    write_index()


# ---------- classify on demand ----------

def stitch_partial(day, cfg):
    """Turn the hour in progress into a video so it can be classified now. The recorder rewrites this file with
    the full hour when the hour ends, keeping the same frame order, so cached descriptions still match."""
    hour = dt.datetime.now().strftime("%H")
    hour_dir = ROOT / "frames" / day / hour
    jpgs = sorted(hour_dir.glob("*.jpg"))[:-1]    # skip the newest frame: it may still be being written
    if not jpgs:
        return
    have = {p.stem for p in jpgs}
    log_path = hour_dir / "log.tsv"
    lines = [l for l in log_path.read_text(errors="ignore").splitlines()
             if l and l.split("\t")[0][11:].replace(":", "") in have] if log_path.exists() else []
    snap = ROOT / "frames" / f".snapshot-{day}-{hour}"
    shutil.rmtree(snap, ignore_errors=True)
    snap.mkdir(parents=True)
    for p in jpgs:
        os.symlink(p, snap / p.name)
    out = VIDEOS / day
    out.mkdir(parents=True, exist_ok=True)
    w, h = 1920, 1200
    r = subprocess.run([cfg["ffmpeg"], "-y", "-loglevel", "error", "-framerate", str(1 / cfg["frameSeconds"]),
                        "-pattern_type", "glob", "-i", str(snap / "*.jpg"),
                        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                        "-c:v", "hevc_videotoolbox", "-q:v", "50", "-tag:v", "hvc1", str(out / f"{hour}.mp4")])
    shutil.rmtree(snap, ignore_errors=True)
    if r.returncode == 0:
        (out / f"{hour}.tsv").write_text("\n".join(lines) + "\n")
        log(f"{day}: added the hour in progress ({len(jpgs)} frames)")


def lock_or_exit():
    """One classifier at a time: the 07:00 job, the catch-up on opening the app, and the button share this lock."""
    ROOT.mkdir(parents=True, exist_ok=True)
    fh = open(ROOT / ".classify.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Already classifying; this run will skip.")
        sys.exit(0)
    return fh


# ---------- main ----------

def process(day, cfg):
    frames = load_hours(day)
    if not frames:
        log(f"{day}: no recordings")
        return False
    out = REPORTS / day
    frames_dir = out / "frames"
    measure_change(day, frames, cfg)
    waste = compute_waste(frames, cfg)
    write_waste_csv(day, waste)
    samples = choose_samples(frames, cfg)
    log(f"{day}: {len(frames)} frames, {len(samples)} samples")
    extract(day, frames, samples, frames_dir, cfg)
    samples = [s for s in samples if (frames_dir / f"{frames[s]['hour']}-{frames[s]['n']:04d}.jpg").exists()]

    obs_path = out / "observations.json"
    obs = json.loads(obs_path.read_text()) if obs_path.exists() else {}
    obs = describe(day, frames, samples, str(frames_dir), cfg, obs)
    for row in obs.values():   # also fixes descriptions cached before normalisation existed
        row["client"] = norm_client(row.get("client"), cfg)
    obs_path.write_text(json.dumps(obs, indent=1))

    tax_path = DATA / "workflows.json"
    taxonomy = json.loads(tax_path.read_text()) if tax_path.exists() else {}
    log(f"{day}: grouping into workflows")
    grouped = group(day, frames, samples, obs, taxonomy, cfg)
    for w in grouped.get("workflows", []):
        taxonomy.setdefault(w["id"], {**w, "first_seen": day})
    segments = build_segments(day, frames, samples, obs, grouped.get("assignments", {}), taxonomy, cfg)
    update_taxonomy(taxonomy, grouped.get("workflows", []), segments, day)
    link_sessions(segments, load_sessions(day), obs, frames)

    (out / "day.json").write_text(json.dumps({"day": day, "segments": segments, "waste": waste}, indent=1))
    write_csv(day, segments)
    write_sessions_csv(day, segments)
    write_report(day, segments, out, waste)
    write_index()
    log(f"{day}: done, {len(segments)} segments -> {out / 'index.html'}")
    return True


def stale(day):
    """No report yet, or recordings added after the report was written (e.g. a mid-day run)."""
    report = REPORTS / day / "day.json"
    if not report.exists():
        return True
    newest = max((p.stat().st_mtime for p in (VIDEOS / day).glob("*.tsv")), default=0)
    return newest > report.stat().st_mtime


def notify(days, verb="Classified"):
    msg = f"{verb} {', '.join(days)}"
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "WorkTape"'], check=False)


def main():
    for d in (REPORTS, DATA):
        d.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    args = sys.argv[1:]
    today = dt.date.today()
    if args and args[0] == "--live":
        live(cfg)
        return
    _lock = lock_or_exit()  # noqa: F841 (held until exit)
    if args and args[0] in ("--catch-up", "--now"):
        # every unclassified or updated day still on disk; --now also does today so far
        days = [(today - dt.timedelta(days=k)).isoformat() for k in range(31, 0, -1)]
        days = [d for d in days if (VIDEOS / d).exists() and stale(d)]
        if args[0] == "--now":
            stitch_partial(today.isoformat(), cfg)
            if (VIDEOS / today.isoformat()).exists():
                days.append(today.isoformat())
        if not days:
            log("Everything is already classified.")
    else:
        days = [args[0] if args else (today - dt.timedelta(days=1)).isoformat()]
    done = []
    for d in days:
        try:
            if process(d, cfg):
                done.append(d)
        except Exception as e:  # keep going with other days; the log has the detail
            log(f"{d}: FAILED {e}")
    if done:
        notify(done)


if __name__ == "__main__":
    main()
