#!/usr/bin/env python3
from __future__ import annotations

"""Harden Dell asset authorization and availability before production selection.

V5 keeps semantic scoring from the proven legacy client while hardening transport:
- one short-lived authorization per downloaded asset;
- oversized legacy catalog authorization is bypassed;
- stale->stale->valid replacement chains keep their semantic mapping correct;
- a live /health response is authoritative, so an old stored heartbeat cannot block a
  healthy persistent Dell gateway;
- only the active ``media/gta_vi`` edit collection (``-mudo.mp4``) is eligible;
- the repository's 593 activity placements are treated as 126 distinct visual cuts,
  so category copies never inflate the unique-media budget;
- scene granularity is lengthened modestly before selection so long scripts stay inside
  the deduplicated clip inventory without reusing a source;
- the eligible owner-curated catalog is availability-probed before semantic selection,
  so dead/stale catalog IDs can never consume one of the required unique scene slots.
"""

import concurrent.futures
import importlib.util
import inspect
import json
import math
import os
import pathlib
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
LEGACY = HERE / "dell_asset_client.py"

spec = importlib.util.spec_from_file_location("mediaforge_dell_asset_client_legacy", LEGACY)
if spec is None or spec.loader is None:
    raise SystemExit("Unable to load Dell asset client")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


def _patch_legacy_multihop_mapping() -> None:
    """Fix stale->stale->valid replacement chains inside legacy.main()."""
    source = inspect.getsource(legacy.main)
    needle = "update_selection_for_replacement(original, replacement, reason)"
    fixed = "update_selection_for_replacement(item, replacement, reason)"
    if needle not in source:
        raise SystemExit("Dell legacy fallback mapping patch no longer matches source; aborting safely")
    exec(source.replace(needle, fixed, 1), legacy.__dict__)


_patch_legacy_multihop_mapping()
_original_control = legacy.control
_original_choose_unique_pool = legacy.choose_unique_pool
_original_segment_script = legacy.segment_script


def _confirm_live_gateway(status: dict) -> dict:
    public_url = str(status.get("public_url") or "").rstrip("/")
    if not public_url:
        return status
    try:
        response = legacy.requests.get(
            f"{public_url}/health",
            headers={"User-Agent": "MediaForge-GitHub/5.6-active-gta-vi-preflight"},
            timeout=12,
        )
        if response.status_code != 200:
            return status
        health = response.json()
        if not health.get("ok") or str(health.get("gateway_id") or "") != "dell-main":
            return status
        live = dict(status)
        live["status"] = "online"
        live["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        live["gateway_live_health"] = True
        live["gateway_version"] = health.get("version") or live.get("gateway_version")
        live["catalog_assets"] = health.get("catalog_assets") or live.get("catalog_assets")
        return live
    except Exception:
        return status


def _control_v5(action: str, body: dict | None = None):
    if action == "create_request" and body is not None:
        asset_ids = list(body.get("asset_ids") or [])
        if len(asset_ids) > 200:
            print(
                json.dumps(
                    {
                        "legacy_catalog_authorization_bypassed": True,
                        "supplied": len(asset_ids),
                        "gateway_max": 200,
                        "reason": "v5 uses single-asset authorization",
                    }
                )
            )
            return {
                "status": "bypassed",
                "request_id": str(body.get("request_id") or ""),
                "asset_count": len(asset_ids),
            }
    result = _original_control(action, body)
    if action == "status" and isinstance(result, dict):
        return _confirm_live_gateway(result)
    return result


legacy.control = _control_v5


def _normalise_path(value: object) -> str:
    return str(value or "").strip().replace("\\", "/").lower().lstrip("./")


def _is_active_edit_asset(asset: dict) -> bool:
    """Match the repository contract from the current GTA VI inventory documents.

    Active edit assets live under ``media/gta_vi`` (or a path relative to that root) and
    are silent edit files ending in ``-mudo.mp4``. Legacy ``stock_videos`` and
    ``stock_images`` remain catalogued source material but are not production inputs.
    """
    values = {
        key: _normalise_path(asset.get(key))
        for key in ("file_name", "relative_path", "path", "source", "collection", "collection_path")
    }
    filename = values["file_name"] or pathlib.PurePosixPath(
        values["relative_path"] or values["source"] or values["path"]
    ).name
    if not filename.endswith("-mudo.mp4"):
        return False

    directory_values = [
        values["relative_path"],
        values["path"],
        values["source"],
        values["collection"],
        values["collection_path"],
    ]
    populated_directories = [value for value in directory_values if value]
    if not populated_directories:
        return True

    for value in populated_directories:
        padded = f"/{value.strip('/')}"
        if (
            "/media/gta_vi/" in padded + "/"
            or padded.startswith("/gta_vi/")
            or padded.startswith("/cortes_por_atividade/")
            or padded.startswith("/por_local/")
            or value == "media/gta_vi"
        ):
            return True
    return False


def _active_edit_assets(assets: list[dict]) -> list[dict]:
    active = [item for item in assets if _is_active_edit_asset(item)]
    baseline = max(1, int(os.getenv("MEDIAFORGE_ACTIVE_GTA_VI_BASELINE", "126")))
    print(
        json.dumps(
            {
                "active_gta_vi_catalog_filter": "complete",
                "raw_catalog_assets": len(assets),
                "active_edit_assets": len(active),
                "excluded_non_edit_assets": len(assets) - len(active),
                "repository_unique_clip_baseline": baseline,
                "repository_organized_edit_files": 604,
                "repository_activity_files": 593,
                "eligible_suffix": "-mudo.mp4",
                "eligible_collection": "media/gta_vi",
            }
        )
    )
    if len(active) < baseline:
        raise SystemExit(
            "Active GTA VI unique edit inventory is below the updated repository baseline before TTS/render: "
            f"active={len(active)}, baseline={baseline}, raw_catalog={len(assets)}. "
            "Refresh the Dell gateway catalog from media/gta_vi; stock_videos and stock_images are not eligible."
        )
    return active


def _delete_request(request_id: str) -> None:
    try:
        legacy.control("delete_request", {"request_id": request_id})
    except Exception:
        pass


def _create_single_asset_request(asset_id: str) -> tuple[str, str]:
    request_id = uuid.uuid4().hex
    token = uuid.uuid4().hex + uuid.uuid4().hex
    expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    legacy.control(
        "create_request",
        {
            "request_id": request_id,
            "request_token": token,
            "job_key": f"single-asset-{asset_id}-{request_id[:8]}",
            "asset_ids": [asset_id],
            "expires_at": expires,
        },
    )
    return request_id, token


def _probe_authorized_asset(public_url: str, request_id: str, token: str, asset: dict) -> tuple[str, bool | None, str]:
    asset_id = str(asset.get("asset_id") or "").strip()
    if not asset_id:
        return asset_id, False, "missing asset_id"
    url = f"{public_url}/asset/{request_id}/{asset_id}"
    transient_codes = {408, 429, 500, 502, 503, 504}
    last_error = "unknown"
    for attempt in range(1, 4):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "X-MediaForge-Request-Token": token,
                    "User-Agent": "MediaForge-GitHub/5.6-active-gta-vi-preflight",
                    "Range": "bytes=0-0",
                },
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                first_byte = response.read(1)
                if first_byte:
                    return asset_id, True, f"HTTP {getattr(response, 'status', 200)}"
                return asset_id, False, "empty asset response"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code in {404, 410}:
                return asset_id, False, last_error
            if exc.code == 403:
                return asset_id, None, "HTTP 403 during catalog availability preflight"
            if exc.code not in transient_codes:
                return asset_id, None, last_error
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < 3:
            time.sleep(attempt)
    return asset_id, None, last_error


def _live_catalog_assets(assets: list[dict]) -> tuple[list[dict], list[str]]:
    if not assets:
        return [], []
    status = legacy.control("status")
    public_url = str(status.get("public_url") or "").rstrip("/")
    if status.get("status") != "online" or not public_url:
        raise SystemExit("Dell asset gateway is offline during catalog availability preflight")

    batch_size = max(1, min(180, int(os.getenv("MEDIAFORGE_ASSET_PROBE_BATCH", "180"))))
    workers = max(1, min(16, int(os.getenv("MEDIAFORGE_ASSET_PROBE_WORKERS", "8"))))
    live_ids: set[str] = set()
    stale_ids: set[str] = set()
    inconclusive: list[tuple[str, str]] = []

    for offset in range(0, len(assets), batch_size):
        batch = assets[offset : offset + batch_size]
        request_id = uuid.uuid4().hex
        token = uuid.uuid4().hex + uuid.uuid4().hex
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        legacy.control(
            "create_request",
            {
                "request_id": request_id,
                "request_token": token,
                "job_key": f"catalog-live-preflight-{request_id[:8]}",
                "asset_ids": [str(item["asset_id"]) for item in batch],
                "expires_at": expires,
            },
        )
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(batch))) as executor:
                futures = [
                    executor.submit(_probe_authorized_asset, public_url, request_id, token, item)
                    for item in batch
                ]
                for future in concurrent.futures.as_completed(futures):
                    asset_id, is_live, reason = future.result()
                    if is_live is True:
                        live_ids.add(asset_id)
                    elif is_live is False:
                        stale_ids.add(asset_id)
                    else:
                        inconclusive.append((asset_id, reason))
        finally:
            _delete_request(request_id)

    if inconclusive:
        sample = ", ".join(f"{asset_id}:{reason}" for asset_id, reason in inconclusive[:8])
        raise SystemExit(
            f"Dell catalog availability preflight was inconclusive for {len(inconclusive)} assets; "
            f"aborting rather than discarding possibly valid media. Sample: {sample}"
        )

    live = [item for item in assets if str(item.get("asset_id")) in live_ids]
    print(
        json.dumps(
            {
                "catalog_availability_preflight": "complete",
                "catalog_total": len(assets),
                "live_assets": len(live),
                "stale_assets": len(stale_ids),
                "probe_workers": workers,
            }
        )
    )
    return live, sorted(stale_ids)


def _scene_plan(script: str, live_count: int) -> tuple[list[str], int, int]:
    words = max(1, len((script or "").split()))
    configured = max(12, min(24, int(os.getenv("MEDIAFORGE_SCENE_TARGET_WORDS", "16"))))
    target = configured
    max_words = max(target + 4, min(30, int(math.ceil(target * 1.5))))
    segments = _original_segment_script(script, target_words=target, max_words=max_words)
    while len(segments) > live_count and target < 24:
        target += 1
        max_words = max(target + 4, min(30, int(math.ceil(target * 1.5))))
        segments = _original_segment_script(script, target_words=target, max_words=max_words)
    if len(segments) > live_count:
        raise SystemExit(
            "Narration still needs more unique GTA VI scenes than the live repository can provide without reuse: "
            f"scenes={len(segments)}, live={live_count}, words={words}, target_words={target}. "
            "Shorten the script or add distinct GTA VI source clips."
        )
    return segments, target, max_words


def choose_unique_pool_live(title: str, script: str, assets: list[dict], max_assets: int, byte_budget: int):
    active_assets = _active_edit_assets(assets)
    live_assets, stale_ids = _live_catalog_assets(active_assets)
    segments, target_words, max_words = _scene_plan(script, len(live_assets))
    if not segments:
        raise SystemExit("Narration could not be segmented for semantic media selection")
    if len(live_assets) < len(segments):
        raise SystemExit(
            "Live GTA VI asset inventory is insufficient before TTS/render: "
            f"live={len(live_assets)}, required={len(segments)}, active_catalog={len(active_assets)}, "
            f"raw_catalog={len(assets)}, stale={len(stale_ids)}. Refresh/reindex the Dell "
            "media/gta_vi collection; reuse remains disabled."
        )

    print(
        json.dumps(
            {
                "semantic_scene_plan": "complete",
                "script_words": len(script.split()),
                "scene_segments": len(segments),
                "scene_target_words": target_words,
                "scene_max_words": max_words,
                "live_unique_assets": len(live_assets),
                "reuse_allowed": False,
            }
        )
    )

    previous_segmenter = legacy.segment_script

    def planned_segmenter(text: str, target_words: int = 12, max_words: int = 18) -> list[str]:
        if text == script:
            return list(segments)
        return _original_segment_script(text, target_words=target_words, max_words=max_words)

    legacy.segment_script = planned_segmenter
    try:
        return _original_choose_unique_pool(title, script, live_assets, max_assets, byte_budget)
    finally:
        legacy.segment_script = previous_segmenter


legacy.choose_unique_pool = choose_unique_pool_live


def download_asset(status: dict, ignored_request_id: str, ignored_token: str, item: dict, out: pathlib.Path):
    del ignored_request_id, ignored_token
    asset_id = str(item["asset_id"])
    current_status = status
    last_error = "unknown"
    transient_codes = {408, 429, 500, 502, 503, 504}

    for auth_attempt in range(1, 4):
        request_id = ""
        token = ""
        part = out.with_suffix(out.suffix + ".part")
        part.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        try:
            request_id, token = _create_single_asset_request(asset_id)
            public_url = str(current_status.get("public_url") or "").rstrip("/")
            if not public_url:
                raise RuntimeError("Dell asset gateway public_url is empty")
            url = f"{public_url}/asset/{request_id}/{asset_id}"
            request = urllib.request.Request(
                url,
                headers={
                    "X-MediaForge-Request-Token": token,
                    "User-Agent": "MediaForge-GitHub/5.6-active-gta-vi-preflight",
                },
            )
            with urllib.request.urlopen(request, timeout=1800) as source, part.open("wb") as target:
                while True:
                    chunk = source.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)

            expected = legacy.asset_size(item)
            actual = part.stat().st_size
            if expected > 0 and actual != expected:
                raise RuntimeError(f"size mismatch: expected={expected} actual={actual}")
            if actual <= 0:
                raise RuntimeError("downloaded file is empty")
            part.replace(out)
            return True, "ok", current_status

        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            part.unlink(missing_ok=True)
            if exc.code in {404, 410}:
                return False, last_error, current_status
            if exc.code == 403:
                if auth_attempt >= 3:
                    raise SystemExit(
                        f"Dell gateway authorization failed three times for asset {asset_id} (HTTP 403). "
                        "Aborting before TTS/render because replacing the video cannot fix gateway auth."
                    )
            elif exc.code not in transient_codes:
                raise SystemExit(f"Dell gateway returned non-recoverable {last_error} for asset {asset_id}")

        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, RuntimeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            part.unlink(missing_ok=True)
            if auth_attempt >= 3:
                return False, last_error, current_status

        finally:
            if request_id:
                _delete_request(request_id)

        try:
            refreshed = legacy.control("status")
            if refreshed.get("status") == "online" and refreshed.get("public_url"):
                current_status = refreshed
        except Exception:
            pass
        time.sleep(auth_attempt)

    return False, last_error, current_status


legacy.download_asset = download_asset
legacy.main()
