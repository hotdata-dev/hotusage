"""hototel (formerly hotusage) constants and hotdata credentials, shared by
server/server.py.

Nothing here parses or prices anything. Transcript parsing, the rate tables and
session building live in the collector, hototel-client's src/core.rs; every
cost_* column arrives already computed in the ingest payload. This module used
to carry a Python copy of all of it, kept for a collector that no longer exists
in this repository -- by then the copy had drifted from the live tables (no fast
mode, stale rates), so what it mostly offered was a second, wrong answer.
"""

import json
import os

# hotdata (hotusage.ai workspace, not Default Workspace: the databases and the
# API key the server runs with belong to it). Two planes:
#   system database   - orgs / users / auth_sessions; each org row records the
#                       org's dedicated usage database id
#   per-org databases - sessions / requests / daily_usage (catalog `hotusage`),
#                       provisioned automatically when an org is created
SYSTEM_DATABASE_ID = "dbidn362d0ry6835of59b6zbio6u2w"
SYSTEM_CATALOG = "hotusage_system"
WORKSPACE_ID = "worky0x9no4fa4p3fmllm0x2m0lu93"
CATALOG = "hotusage"
HOTDATA_KEY_FILE = os.path.expanduser("~/.hotdata/hotdata.json")


def hotdata_api_key():
    key = os.environ.get("HOTDATA_API_KEY")
    if key:
        return key
    try:
        with open(HOTDATA_KEY_FILE) as f:
            return json.load(f)["api_key"]
    except (OSError, ValueError, KeyError):
        return None
