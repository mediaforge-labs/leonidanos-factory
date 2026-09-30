#!/usr/bin/env python3
from __future__ import annotations

"""Fail-fast contract gate for the current Dell GTA VI repository.

The authoritative editing repository is ``media/gta_vi``. Production is allowed to
continue only when the Dell gateway exposes the v3 active catalog, every eligible item
is a silent ``-mudo.mp4`` edit asset under that collection, and the public gateway
health endpoint agrees with the stored catalog. After the contract is proven, the
hardened v5 client performs live availability probing, semantic selection and download.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import requests

EXPECTED_GATEWAY_VERSION = "3.2-active-gta-vi-rest"
EXPECTED_SCOPE = "media/gta_vi-active-edit-only"
EXPECTED_COLLECTION = "media/gta_vi"
EXPECTED_SUFFIX = "-mudo.mp4"
MIN_UNIQUE_ASSETS = 126


def control(action: str) -> dict:
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SECRET_KEY"]
    response = requests.get(
        f"{base}/functions/v1/mediaforge-dell-control",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "User-Agent": "MediaForge-GitHub/6.1-repository-contract",
        },
        params={"action": action},
        timeout=60,
    )
    if response.status_code >= 400:
        raise SystemExit(f"Dell control {action} failed ({response.status_code}): {response.text[:500]}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise SystemExit(f"Dell control {action} returned a non-object payload")
    return payload


def normalise(value: object) -> str:
    return str(value or "").replace("\\", "/").strip().lower().lstrip("./")


def fetch_live_health(status: dict) -> dict:
    public_url = str(status.get("public_url") or "").rstrip("/")
    if not public_url:
        raise SystemExit("Dell gateway public_url is empty before production")
    try:
        response = requests.get(
            public_url + "/health",
            headers={"User-Agent": "MediaForge-GitHub/6.1-repository-contract"},
            timeout=20,
        )
        response.raise_for_status()
        health = response.json()
    except Exception as exc:
        raise SystemExit(f"Dell public gateway health check failed before production: {exc}") from exc
    if not isinstance(health, dict) or health.get("ok") is not True:
        raise SystemExit("Dell public gateway /health did not return ok=true")
    return health


def validate_catalog(status: dict, catalog: dict) -> None:
    if status.get("status") != "online" or not status.get("public_url"):
        raise SystemExit("Dell gateway is not online before production")

    # The control-row heartbeat is eventually consistent and can become stale even while
    # the persistent Dell gateway/tunnel is serving traffic normally. Probe the live
    # endpoint first; a healthy v3 endpoint is authoritative for liveness. We still log
    # whether the stored heartbeat was fresh so control-plane drift remains observable.
    health = fetch_live_health(status)
    if str(health.get("version") or "") != EXPECTED_GATEWAY_VERSION:
        raise SystemExit(
            f"Dell live gateway is obsolete: health.version={health.get('version')!r}, "
            f"expected={EXPECTED_GATEWAY_VERSION!r}"
        )

    heartbeat = str(status.get("heartbeat_at") or "")
    heartbeat_fresh = False
    try:
        beat = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
        heartbeat_fresh = datetime.now(timezone.utc) - beat <= timedelta(minutes=2)
    except Exception:
        heartbeat_fresh = False

    gateway_version = str(catalog.get("gateway_version") or status.get("gateway_version") or "")
    if gateway_version != EXPECTED_GATEWAY_VERSION:
        raise SystemExit(
            "Dell catalog is from an obsolete repository layout: "
            f"gateway_version={gateway_version!r}, expected={EXPECTED_GATEWAY_VERSION!r}. "
            "Run tools/setup_dell_asset_gateway.ps1 on the Dell after git pull."
        )

    scope = str(catalog.get("scope") or "")
    if scope != EXPECTED_SCOPE:
        raise SystemExit(
            "Dell catalog scope is not the active GTA VI editing repository: "
            f"scope={scope!r}, expected={EXPECTED_SCOPE!r}."
        )

    assets = list(catalog.get("assets") or [])
    baseline = max(MIN_UNIQUE_ASSETS, int(os.getenv("MEDIAFORGE_ACTIVE_GTA_VI_BASELINE", str(MIN_UNIQUE_ASSETS))))
    if len(assets) < baseline:
        raise SystemExit(
            f"Dell active repository has only {len(assets)} unique edit assets; baseline is {baseline}."
        )

    violations: list[str] = []
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for item in assets:
        aid = str(item.get("asset_id") or "").strip()
        rel = normalise(item.get("relative_path"))
        name = normalise(item.get("file_name"))
        collection = normalise(item.get("collection"))
        quick_hash = str(item.get("quick_hash") or "").strip().lower()

        if not aid or aid in seen_ids:
            violations.append(f"duplicate/missing asset_id:{aid or '<empty>'}")
        seen_ids.add(aid)
        if quick_hash:
            if quick_hash in seen_hashes:
                violations.append(f"duplicate visual quick_hash:{quick_hash[:12]}")
            seen_hashes.add(quick_hash)
        if collection != EXPECTED_COLLECTION:
            violations.append(f"collection:{collection or '<empty>'}")
        if not rel.startswith("media/gta_vi/"):
            violations.append(f"relative_path:{rel or '<empty>'}")
        if "/stock_videos/" in f"/{rel}/" or "/stock_images/" in f"/{rel}/":
            violations.append(f"legacy_source:{rel}")
        if not name.endswith(EXPECTED_SUFFIX):
            violations.append(f"file_name:{name or '<empty>'}")
        if item.get("audio") not in {None, "mudo"}:
            violations.append(f"audio:{item.get('audio')}")
        if len(violations) >= 12:
            break

    if violations:
        raise SystemExit(
            "Dell active catalog violates the repository contract; first violations: "
            + "; ".join(violations)
        )

    health_count = int(health.get("catalog_assets") or 0)
    if health_count != len(assets):
        raise SystemExit(
            "Dell gateway memory/catalog mismatch before production: "
            f"health={health_count}, stored_catalog={len(assets)}. Restart the gateway after catalog refresh."
        )

    print(
        json.dumps(
            {
                "repository_contract": "validated",
                "gateway_version": gateway_version,
                "scope": scope,
                "unique_edit_assets": len(assets),
                "collection": EXPECTED_COLLECTION,
                "suffix": EXPECTED_SUFFIX,
                "legacy_stock_allowed": False,
                "live_health_authoritative": True,
                "stored_heartbeat_fresh": heartbeat_fresh,
            },
            ensure_ascii=False,
        )
    )


def main() -> int:
    status = control("status")
    catalog = control("catalog")
    validate_catalog(status, catalog)
    cmd = [sys.executable, "public-worker-template/dell_asset_client_v5.py", *sys.argv[1:]]
    return subprocess.call(cmd, env=os.environ.copy())


if __name__ == "__main__":
    raise SystemExit(main())
