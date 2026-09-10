#!/usr/bin/env python3
"""hotusage collector — macOS menu bar agent.

Sits in the top menu bar (like Docker), periodically parses this machine's AI
coding-agent history (Claude Code, Codex, OpenCode) and sends changed sessions
to the central hotusage server, which writes them into hotdata.

Run modes:
    python3 collector.py            # menu bar app (needs `pip install rumps`)
    python3 collector.py --once     # headless one-shot sync (stdlib only)

Config:  ~/.hotusage/collector.json   (created with defaults on first run)
State:   ~/.hotusage/collector-state.json  (per-session fingerprints already sent)
"""

import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

CONFIG_PATH = os.path.join(core.CONFIG_DIR, "collector.json")
STATE_PATH = os.path.join(core.CONFIG_DIR, "collector-state.json")

DEFAULT_CONFIG = {
    "server_url": "http://127.0.0.1:8377",
    "token": "",
    "user_email": "",
    "interval_minutes": 15,
}


def guess_email():
    try:
        out = subprocess.run(["git", "config", "user.email"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return os.environ.get("USER", "unknown") + "@" + socket.gethostname()


def load_config():
    os.makedirs(core.CONFIG_DIR, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    if not cfg["user_email"]:
        cfg["user_email"] = guess_email()
    if not os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    return cfg


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(core.CONFIG_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def session_rows(built):
    """Convert one built session to wire rows (server stamps user/host)."""
    s = built["summary"]
    session = {
        "session_id": s["id"],
        "provider": s["provider"],
        "project": s["project"],
        "cwd": s["cwd"],
        "title": s["title"],
        "started_at": s["start"],
        "ended_at": s["end"],
        "requests": s["requests"],
        "models": ",".join(s["models"]),
        "input_tokens": s["in"],
        "output_tokens": s["out"],
        "cache_read_tokens": s["cr"],
        "cache_write_tokens": s["cw"],
        "cost_input": s["cin"],
        "cost_output": s["cout"],
        "cost_cache_read": s["ccr"],
        "cost_cache_write": s["ccw"],
        "cost_total": s["cost"],
        "peak_context_tokens": s["peakCtx"],
    }
    requests = [{
        "session_id": s["id"], "provider": s["provider"], "seq": i,
        "ts": p["t"], "context_tokens": p["ctx"], "output_tokens": p["out"],
    } for i, p in enumerate(built["detail"], 1)]
    daily = [{
        "session_id": s["id"], "provider": s["provider"], "day": r["d"],
        "input_tokens": r["in"], "output_tokens": r["out"],
        "cache_read_tokens": r["cr"], "cache_write_tokens": r["cw"],
        "cost_input": r["cin"], "cost_output": r["cout"],
        "cost_cache_read": r["ccr"], "cost_cache_write": r["ccw"],
    } for r in built["daily"]]
    return session, requests, daily


class Collector:
    def __init__(self, config):
        self.config = config
        self.scanner = core.LocalScanner()
        self.hostname = socket.gethostname()
        self.last_result = "never synced"
        self.last_time = None
        self.lock = threading.Lock()

    def fingerprint(self, built):
        s = built["summary"]
        return f'{s["end"]}|{s["requests"]}'

    def sync(self):
        """Parse local history, send changed sessions. Returns a status string."""
        with self.lock:
            try:
                built = self.scanner.scan()
                state = load_state()
                changed = []
                for b in built:
                    key = b["summary"]["provider"] + ":" + b["summary"]["id"]
                    fp = self.fingerprint(b)
                    if state.get(key) != fp:
                        changed.append((key, fp, b))
                if not changed:
                    self.last_result = f"up to date ({len(built)} sessions)"
                    self.last_time = datetime.now()
                    return self.last_result

                payload = {"schema": 1, "user_email": self.config["user_email"],
                           "hostname": self.hostname, "sessions": [], "requests": [], "daily": []}
                for _, _, b in changed:
                    sess, reqs, daily = session_rows(b)
                    payload["sessions"].append(sess)
                    payload["requests"].extend(reqs)
                    payload["daily"].extend(daily)

                url = self.config["server_url"].rstrip("/") + "/ingest"
                req = urllib.request.Request(
                    url, data=json.dumps(payload).encode(), method="POST",
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + (self.config["token"] or "")})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = json.loads(resp.read())
                for key, fp, _ in changed:
                    state[key] = fp
                save_state(state)
                self.last_result = (f"sent {body.get('sessions', len(changed))} changed "
                                    f"of {len(built)} sessions")
                self.last_time = datetime.now()
                return self.last_result
            except urllib.error.HTTPError as e:
                self.last_result = f"server error {e.code}: {e.read()[:120].decode(errors='replace')}"
            except Exception as e:
                self.last_result = f"failed: {e}"
            self.last_time = datetime.now()
            return self.last_result


def run_menu_bar(collector):
    try:
        import rumps
    except ImportError:
        sys.exit("rumps is required for the menu bar app: pip install rumps\n"
                 "(or run a one-shot sync with: python3 collector.py --once)")

    class HotusageApp(rumps.App):
        def __init__(self):
            super().__init__("hotusage", title="⏶", quit_button="Quit hotusage")
            self.status_item = rumps.MenuItem("Starting...")
            self.user_item = rumps.MenuItem(
                f'{collector.config["user_email"]} on {collector.hostname}')
            self.menu = [self.status_item, self.user_item, None,
                         "Sync Now", "Open Dashboard", "Edit Config", None]
            interval = max(1, int(collector.config.get("interval_minutes", 15))) * 60
            self.timer = rumps.Timer(self.tick, interval)
            self.timer.start()
            threading.Thread(target=self.do_sync, daemon=True).start()

        def refresh_status(self):
            t = collector.last_time.strftime("%H:%M") if collector.last_time else "-"
            self.status_item.title = f"Last sync {t}: {collector.last_result}"

        def do_sync(self):
            self.status_item.title = "Syncing..."
            collector.sync()
            self.refresh_status()

        def tick(self, _timer):
            threading.Thread(target=self.do_sync, daemon=True).start()

        @rumps.clicked("Sync Now")
        def sync_now(self, _):
            threading.Thread(target=self.do_sync, daemon=True).start()

        @rumps.clicked("Open Dashboard")
        def open_dashboard(self, _):
            import webbrowser
            webbrowser.open(collector.config["server_url"])

        @rumps.clicked("Edit Config")
        def edit_config(self, _):
            subprocess.run(["open", "-t", CONFIG_PATH])

    HotusageApp().run()


def main():
    config = load_config()
    collector = Collector(config)
    if "--once" in sys.argv:
        print(f"hotusage collector: {config['user_email']} -> {config['server_url']}")
        print("hotusage collector:", collector.sync())
        sys.exit(0 if not collector.last_result.startswith(("failed", "server error")) else 1)
    run_menu_bar(collector)


if __name__ == "__main__":
    main()
