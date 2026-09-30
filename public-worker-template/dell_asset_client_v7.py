#!/usr/bin/env python3
from __future__ import annotations

"""Resilient contract gate for Dell asset gateway v3.2.

V7 keeps the strict v6 repository contract and tolerates control-plane drift. The
live gateway /health endpoint is authoritative for liveness; the stored heartbeat is
only a hint that can legitimately be stale while a persistent tunnel keeps serving.
"""

import importlib.util
import pathlib
import time

HERE = pathlib.Path(__file__).resolve().parent
V6 = HERE / "dell_asset_client_v6.py"

spec = importlib.util.spec_from_file_location("mediaforge_dell_asset_client_v6", V6)
if spec is None or spec.loader is None:
    raise SystemExit("Unable to load Dell asset client v6")
v6 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v6)

_original_fetch_live_health = v6.fetch_live_health


def resilient_fetch_live_health(status: dict) -> dict:
    current = dict(status)
    last_error = "unknown"

    # Always probe the currently published public_url first, even when the control
    # heartbeat is stale. v6 already validates that /health returns ok=true and that
    # the live gateway is the expected v3.2 implementation. If the endpoint is really
    # gone (for example after a Quick Tunnel rotation), refresh control state and retry.
    for attempt in range(1, 7):
        try:
            health = _original_fetch_live_health(current)
            status.clear()
            status.update(current)
            return health
        except SystemExit as exc:
            last_error = str(exc)

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
        f"Last state: {last_error}. Ensure the Dell gateway/tunnel is reachable and, "
        "if its endpoint changed, republish the current status from the workstation."
    )


v6.fetch_live_health = resilient_fetch_live_health


def main() -> int:
    return v6.main()


if __name__ == "__main__":
    raise SystemExit(main())
