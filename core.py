"""hotusage core — shared parsing, pricing, and session-building library.

Used by:
  - collector/collector.py  (parses this machine's agent history)
  - server/server.py        (constants, hotdata auth helpers)

Providers parsed: Claude Code (~/.claude/projects), Codex (~/.codex/sessions),
OpenCode (~/.local/share/opencode/opencode.db). Cursor and Gemini CLI store no
local token counts, so there is nothing to parse for them.
"""

import glob
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone

CLAUDE_DIR = os.path.expanduser("~/.claude/projects")
CODEX_DIR = os.path.expanduser("~/.codex/sessions")
CODEX_INDEX = os.path.expanduser("~/.codex/session_index.jsonl")
OPENCODE_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")

# hotdata (prod Default Workspace). Two planes:
#   system database  - orgs / users / auth_sessions; each org row records the
#                      org's dedicated usage database id
#   per-org databases - sessions / requests / daily_usage (catalog `hotusage`),
#                      provisioned automatically when an org is created
SYSTEM_DATABASE_ID = "dbidn362d0ry6835of59b6zbio6u2w"
SYSTEM_CATALOG = "hotusage_system"
DATABASE_ID = "dbid4bldth2f88j78dxybqyinep75e"  # the hotdata org's usage db
# hotusage lives in its own workspace ("hotusage.ai"), not Default Workspace:
# the databases and the API key the server runs with belong to it.
WORKSPACE_ID = "worky0x9no4fa4p3fmllm0x2m0lu93"
CATALOG = "hotusage"
HOTDATA_KEY_FILE = os.path.expanduser("~/.hotdata/hotdata.json")

CONFIG_DIR = os.path.expanduser("~/.hotusage")


def hotdata_api_key():
    key = os.environ.get("HOTDATA_API_KEY")
    if key:
        return key
    try:
        with open(HOTDATA_KEY_FILE) as f:
            return json.load(f)["api_key"]
    except (OSError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Pricing (USD per million tokens, provider list prices as of Sep 2026).
#
# NOT USED BY THE SERVER. server.py imports this module only for the hotdata
# ids, catalog names and API key; it never prices anything. Every cost_* column
# is computed on the client and arrives already priced in the ingest payload, so
# editing the rates here changes nothing anyone sees -- the live table is
# `rates_claude` / `rates_claude_fast` / `rates_openai` in hotusage-client's
# src/core.rs. This copy is a leftover from the original Python collector and is
# kept only so that collector still runs; it has already drifted (it has no fast
# mode branch, which the client now prices at a premium).
#
# These are list-price equivalents either way -- subscription plans bill a flat
# per-seat fee and none of this.
# ---------------------------------------------------------------------------
def rates_claude(model):
    """(input, output) per MTok. Cache: read 0.1x, 5m write 1.25x, 1h write 2x."""
    m = (model or "").lower()
    if "fable" in m or "mythos" in m:
        return (10.0, 50.0)
    if "opus-4-1" in m or "opus-4-2025" in m or "claude-3-opus" in m:
        return (15.0, 75.0)
    if "opus" in m:
        return (5.0, 25.0)
    if "sonnet" in m:
        return (3.0, 15.0)
    if "haiku-3-5" in m or "haiku-3.5" in m:
        return (0.8, 4.0)
    if "haiku-3" in m:
        return (0.25, 1.25)
    if "haiku" in m:
        return (1.0, 5.0)
    return (0.0, 0.0)  # <synthetic>, unknown

# (prefix, input, cached input, output) — longest/most specific prefixes first
OPENAI_RATES = [
    ("gpt-5.6-sol", 5.0, 0.5, 30.0),
    ("gpt-5.6-terra", 2.0, 0.2, 12.0),
    ("gpt-5.6-luna", 0.2, 0.02, 1.2),
    ("gpt-6-astra", 10.0, 1.0, 50.0),
    ("astra", 10.0, 1.0, 50.0),
    ("gpt-5-mini", 0.25, 0.025, 2.0),
    ("gpt-5-nano", 0.05, 0.005, 0.4),
    ("gpt-5", 1.25, 0.125, 10.0),   # also gpt-5.x-codex variants fall through here
    ("codex-mini", 1.5, 0.375, 6.0),
    ("o3", 2.0, 0.5, 8.0),
    ("o4-mini", 1.1, 0.275, 4.4),
    ("gpt-4.1", 2.0, 0.5, 8.0),
    ("gpt-4o", 2.5, 1.25, 10.0),
]

def rates_openai(model):
    m = (model or "").lower()
    for prefix, rin, rcache, rout in OPENAI_RATES:
        if m.startswith(prefix):
            return (rin, rcache, rout)
    return (0.0, 0.0, 0.0)


def msg_costs(provider, m):
    """Cost split by token type: (input, output, cache read, cache write).

    A normalized message may carry a precomputed 'costs' tuple instead.
    """
    if m.get("costs") is not None:
        return m["costs"]
    model = m.get("model") or ""
    if provider == "claude" or "claude" in model.lower():
        rin, rout = rates_claude(model)
        return (m["in"] * rin / 1e6,
                m["out"] * rout / 1e6,
                m["cr"] * rin * 0.1 / 1e6,
                (m["cw5"] * 1.25 + m["cw1"] * 2.0) * rin / 1e6)
    rin, rcache, rout = rates_openai(model)
    # OpenAI-style: cached reads discounted, cache writes not billed separately
    return (m["in"] * rin / 1e6,
            m["out"] * rout / 1e6,
            m["cr"] * rcache / 1e6,
            0.0)


def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def ms_to_iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def project_name(cwd, fallback):
    if cwd:
        base = os.path.basename(cwd.rstrip("/"))
        if base:
            return base
    return fallback


# ---------------------------------------------------------------------------
# Normalized parse results.
# Each parser returns raw tuples: (provider, session_id, cwd, title, msgs)
# where each msg is {ts, model, in, out, cr, cw5, cw1} plus optional
# 'ctx' (context tokens for that request) and 'costs' (precomputed 4-tuple).
# build_session() turns merged raw tuples into the shared shape.
# ---------------------------------------------------------------------------
def build_session(provider, session_id, cwd, title, msgs, dirname="?"):
    if not msgs:
        return None
    msgs = sorted(msgs, key=lambda r: r["ts"] or "")
    tot = {"in": 0, "out": 0, "cr": 0, "cw": 0}
    costs = [0.0, 0.0, 0.0, 0.0]
    peak_ctx = 0
    models = set()
    daily = {}
    detail = []

    for r in msgs:
        cw = r["cw5"] + r["cw1"]
        ctx = r.get("ctx")
        if ctx is None:
            ctx = r["in"] + r["cr"] + cw
        c = msg_costs(provider, r)
        tot["in"] += r["in"]
        tot["out"] += r["out"]
        tot["cr"] += r["cr"]
        tot["cw"] += cw
        for i in range(4):
            costs[i] += c[i]
        peak_ctx = max(peak_ctx, ctx)
        if r.get("model") and r["model"] != "<synthetic>":
            models.add(r["model"])
        dt = parse_ts(r["ts"])
        if dt:
            day = dt.astimezone().strftime("%Y-%m-%d")
            d = daily.setdefault(day, {"in": 0, "out": 0, "cr": 0, "cw": 0,
                                       "cin": 0.0, "cout": 0.0, "ccr": 0.0, "ccw": 0.0})
            d["in"] += r["in"]
            d["out"] += r["out"]
            d["cr"] += r["cr"]
            d["cw"] += cw
            d["cin"] += c[0]
            d["cout"] += c[1]
            d["ccr"] += c[2]
            d["ccw"] += c[3]
        detail.append({"t": r["ts"], "ctx": ctx, "out": r["out"]})

    return {
        "summary": {
            "id": session_id,
            "provider": provider,
            "project": project_name(cwd, dirname),
            "cwd": cwd or dirname,
            "title": title or "(untitled session)",
            "start": msgs[0]["ts"],
            "end": msgs[-1]["ts"],
            "requests": len(msgs),
            "models": sorted(models),
            "in": tot["in"],
            "out": tot["out"],
            "cr": tot["cr"],
            "cw": tot["cw"],
            "cin": round(costs[0], 4),
            "cout": round(costs[1], 4),
            "ccr": round(costs[2], 4),
            "ccw": round(costs[3], 4),
            "cost": round(sum(costs), 4),
            "peakCtx": peak_ctx,
        },
        "daily": [{"d": day, **{k: (round(v, 4) if k in ("cin", "cout", "ccr", "ccw") else v)
                                for k, v in agg.items()}}
                  for day, agg in sorted(daily.items())],
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Claude Code. Assistant records repeat the same message.id (and identical
# usage) once per content block, so usage is deduped by message.id.
# ---------------------------------------------------------------------------
def parse_claude_file(path):
    dirname = os.path.basename(os.path.dirname(path))
    session_id = os.path.splitext(os.path.basename(path))[0]
    msgs = {}
    title = None
    first_prompt = None
    cwd_counts = {}

    with open(path, errors="replace") as f:
        for line in f:
            if '"assistant"' in line:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") != "assistant":
                    continue
                m = o.get("message") or {}
                u = m.get("usage")
                mid = m.get("id")
                if not u or not mid or mid in msgs:
                    continue
                cw = u.get("cache_creation") or {}
                cw5 = cw.get("ephemeral_5m_input_tokens")
                cw1 = cw.get("ephemeral_1h_input_tokens")
                if cw5 is None and cw1 is None:
                    cw5 = u.get("cache_creation_input_tokens") or 0
                    cw1 = 0
                msgs[mid] = {
                    "ts": o.get("timestamp"),
                    "model": m.get("model") or "",
                    "in": u.get("input_tokens") or 0,
                    "out": u.get("output_tokens") or 0,
                    "cr": u.get("cache_read_input_tokens") or 0,
                    "cw5": cw5 or 0,
                    "cw1": cw1 or 0,
                }
                cwd = o.get("cwd")
                if cwd:
                    cwd_counts[cwd] = cwd_counts.get(cwd, 0) + 1
            elif '"ai-title"' in line and title is None:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") == "ai-title":
                    title = o.get("aiTitle")
            elif first_prompt is None and '"user"' in line:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") == "user":
                    content = (o.get("message") or {}).get("content")
                    if isinstance(content, str) and content.strip():
                        first_prompt = content.strip()

    if not msgs:
        return []
    if not title and first_prompt:
        title = first_prompt.splitlines()[0][:80]
    cwd = max(cwd_counts, key=cwd_counts.get) if cwd_counts else None
    return [("claude", session_id, cwd or dirname, title, list(msgs.values()))]


# ---------------------------------------------------------------------------
# Codex. token_count events carry cumulative totals; we take deltas between
# events so the sums stay correct even when an event covers several requests.
# input_tokens includes cached_input_tokens (OpenAI semantics), so
# in = input - cached, cache read = cached, and context = last input_tokens.
# ---------------------------------------------------------------------------
CODEX_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")

def parse_codex_file(path, titles):
    session_id = None
    cwd = None
    first_prompt = None
    model = ""
    prev = None
    msgs = []

    with open(path, errors="replace") as f:
        for line in f:
            if '"turn_context"' in line:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") == "turn_context":
                    model = (o.get("payload") or {}).get("model") or model
            elif '"session_meta"' in line and session_id is None:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") == "session_meta":
                    p = o.get("payload") or {}
                    session_id = p.get("id") or p.get("session_id")
                    cwd = p.get("cwd")
            elif '"token_count"' in line:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") != "event_msg":
                    continue
                p = o.get("payload") or {}
                if p.get("type") != "token_count":
                    continue
                info = p.get("info")
                if not info:
                    continue
                tot = info.get("total_token_usage") or {}
                last = info.get("last_token_usage") or {}
                if prev is None:
                    delta = {k: tot.get(k, 0) for k in CODEX_USAGE_KEYS}
                else:
                    delta = {k: tot.get(k, 0) - prev.get(k, 0) for k in CODEX_USAGE_KEYS}
                    if any(v < 0 for v in delta.values()):  # counter reset
                        delta = {k: last.get(k, 0) for k in CODEX_USAGE_KEYS}
                prev = tot
                cached = max(delta["cached_input_tokens"], 0)
                msgs.append({
                    "ts": o.get("timestamp"),
                    "model": model,
                    "in": max(delta["input_tokens"] - cached, 0),
                    "out": max(delta["output_tokens"], 0),
                    "cr": cached,
                    "cw5": 0, "cw1": 0,
                    "ctx": last.get("input_tokens", 0),
                })
            elif first_prompt is None and '"user_message"' in line:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                p = o.get("payload") or {}
                if o.get("type") == "event_msg" and p.get("type") == "user_message":
                    txt = p.get("message")
                    if isinstance(txt, str) and txt.strip():
                        first_prompt = txt.strip()

    if not msgs:
        return []
    if not session_id:
        session_id = os.path.splitext(os.path.basename(path))[0]
    title = titles.get(session_id)
    if not title and first_prompt:
        title = first_prompt.splitlines()[0][:80]
    return [("codex", session_id, cwd, title, msgs)]


def load_codex_titles():
    titles = {}
    try:
        with open(CODEX_INDEX, errors="replace") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("id") and o.get("thread_name"):
                    titles[o["id"]] = o["thread_name"]
    except OSError:
        pass
    return titles


# ---------------------------------------------------------------------------
# OpenCode. SQLite: session rows for metadata, message rows (JSON blobs) for
# per-request tokens. opencode's own per-message cost is used when our rate
# table doesn't know the model.
# ---------------------------------------------------------------------------
def parse_opencode_db(db_path):
    out = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        cur = con.cursor()
        sessions = cur.execute(
            "SELECT id, title, directory FROM session WHERE parent_id IS NULL").fetchall()
        for sid, title, directory in sessions:
            msgs = []
            for (data,) in cur.execute(
                    "SELECT data FROM message WHERE session_id = ? ORDER BY time_created", (sid,)):
                try:
                    d = json.loads(data)
                except ValueError:
                    continue
                if d.get("role") != "assistant":
                    continue
                tk = d.get("tokens") or {}
                cache = tk.get("cache") or {}
                created = (d.get("time") or {}).get("created")
                if not created or not tk:
                    continue
                model = d.get("modelID") or ""
                provider_id = (d.get("providerID") or "").lower()
                m = {
                    "ts": ms_to_iso(created),
                    "model": model,
                    "in": tk.get("input") or 0,
                    "out": (tk.get("output") or 0) + (tk.get("reasoning") or 0),
                    "cr": cache.get("read") or 0,
                    "cw5": cache.get("write") or 0,
                    "cw1": 0,
                }
                # our tables know anthropic/openai models; otherwise fall back
                # to opencode's own computed cost, bucketed under output
                known = (rates_claude(model) != (0.0, 0.0) if "anthropic" in provider_id or "claude" in model.lower()
                         else rates_openai(model) != (0.0, 0.0, 0.0))
                if not known and d.get("cost"):
                    m["costs"] = (0.0, float(d["cost"]), 0.0, 0.0)
                msgs.append(m)
            if msgs:
                out.append(("opencode", sid, directory, title, msgs))
    except sqlite3.Error as e:
        print(f"warn: opencode db: {e}", file=sys.stderr)
    finally:
        con.close()
    return out


# ---------------------------------------------------------------------------
# LocalScanner: scans all providers on this machine, memoized per source file
# on (mtime, size). Used by the collector.
# ---------------------------------------------------------------------------
def file_key(path):
    st = os.stat(path)
    return (st.st_mtime, st.st_size)


class LocalScanner:
    def __init__(self):
        self.cache = {}       # source path -> (key, [raw tuples])
        self.codex_titles = {}
        self.codex_index_key = None
        self.lock = threading.Lock()

    def _memo(self, path, parse_fn):
        try:
            key = file_key(path)
        except OSError:
            return []
        hit = self.cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        try:
            raw = parse_fn(path)
        except Exception as e:
            print(f"warn: failed to parse {path}: {e}", file=sys.stderr)
            raw = []
        self.cache[path] = (key, raw)
        return raw

    def scan(self):
        """Returns built session dicts (see build_session) for this machine."""
        with self.lock:
            raws = []
            live = set()

            try:
                ikey = file_key(CODEX_INDEX)
            except OSError:
                ikey = None
            if ikey != self.codex_index_key:
                self.codex_titles = load_codex_titles()
                self.codex_index_key = ikey
                for p in list(self.cache):
                    if p.startswith(CODEX_DIR):
                        del self.cache[p]

            for p in glob.glob(os.path.join(CLAUDE_DIR, "*", "*.jsonl")):
                live.add(p)
                raws.extend(self._memo(p, parse_claude_file))
            for p in glob.glob(os.path.join(CODEX_DIR, "*", "*", "*", "*.jsonl")):
                live.add(p)
                raws.extend(self._memo(p, lambda fp: parse_codex_file(fp, self.codex_titles)))
            if os.path.isfile(OPENCODE_DB):
                live.add(OPENCODE_DB)
                wal = OPENCODE_DB + "-wal"
                wal_key = file_key(wal) if os.path.isfile(wal) else None
                hit = self.cache.get(OPENCODE_DB)
                key = (file_key(OPENCODE_DB), wal_key)
                if hit and hit[0] == key:
                    raws.extend(hit[1])
                else:
                    raw = parse_opencode_db(OPENCODE_DB)
                    self.cache[OPENCODE_DB] = (key, raw)
                    raws.extend(raw)

            for p in list(self.cache):
                if p not in live:
                    del self.cache[p]

            # merge raw tuples sharing (provider, id) — e.g. resumed codex rollouts
            merged = {}
            for provider, sid, cwd, title, msgs in raws:
                key = (provider, sid)
                if key in merged:
                    prev = merged[key]
                    merged[key] = (provider, sid, prev[2] or cwd, prev[3] or title,
                                   prev[4] + msgs)
                else:
                    merged[key] = (provider, sid, cwd, title, list(msgs))

            built = []
            for provider, sid, cwd, title, msgs in merged.values():
                s = build_session(provider, sid, cwd, title, msgs)
                if s:
                    built.append(s)
            return built
