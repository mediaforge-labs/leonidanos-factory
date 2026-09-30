#!/usr/bin/env python3
from __future__ import annotations

"""Resilient contract gate for Dell asset gateway v3.2.

V7 keeps the strict v6 repository contract, but tolerates the brief control-plane race
that occurs when a Cloudflare Quick Tunnel is renewed and receives a new public URL.
"""

import importlib.util
import pathlib
import time
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
V6 = HERE / "dell_asset_client_v6.py"

spec = importlib.util.spec_from_file_location("mediaforge_dell_asset_client_v6", V6)
if spec is None or spec.loader is None:
    raise SystemExit("Unable to load Dell asset client v6")
v6 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v6)

_original_fetch_live_health = v6.fetch_live_health


def _heartbeat_fresh(status: dict, max_age_seconds: int = 120) -> bool:
    raw = str(status.get("heartbeat_at") or "")
    try:
        beat = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - beat).total_seconds() <= max_age_seconds
    except Exception:
        return False


def resilient_fetch_live_health(status: dict) -> dict:
    current = dict(status)
    last_error = "unknown"

    # Six passes cover tunnel rotation + the 20s Dell heartbeat interval without
    # hiding a genuinely offline workstation for several minutes.
    for attempt in range(1, 7):
        if _heartbeat_fresh(current):
            try:
                health = _original_fetch_live_health(current)
                status.clear()
                status.update(current)
                return health
            except SystemExit as exc:
                last_error = str(exc)
        else:
            last_error = (
                "Dell control heartbeat is stale; waiting for the workstation gateway "
                "to publish a fresh endpoint"
            )

        if attempt == 6:
            break

        time.sleep(5)
        try:
            fresh = v6.control("status")
            if isinstance(fresh, dict):
                current = fresh
                status.clear()
                status.update(fresh)
        except Exception as exc:
            last_error = f"Dell control refresh failed: {exc}"

    raise SystemExit(
        "Dell asset gateway did not become healthy after automatic endpoint recovery. "
        f"Last state: {last_error}. Ensure the Dell is powered on/logged in and run "
        "tools/setup_dell_asset_gateway_v32.ps1 once if the gateway was upgraded."
    )


v6.fetch_live_health = resilient_fetch_live_health


def main() -> int:
    return v6.main()


if __name__ == "__main__":
    raise SystemExit(main())
