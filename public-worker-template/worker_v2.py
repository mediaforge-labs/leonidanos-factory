#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib

import worker as legacy


def verify_external_unique_media() -> dict[str, int | bool | str]:
    root_raw = os.environ.get("MEDIAFORGE_VIDEO_LIBRARY_ROOT", "").strip()
    if not root_raw:
        raise SystemExit(
            "Strict unique-media verification requires MEDIAFORGE_VIDEO_LIBRARY_ROOT "
            "when the private core has no internal use_counts selector"
        )

    root = pathlib.Path(root_raw).resolve()
    selection_path = root / "mediaforge-selection.json"
    catalog_path = root / "Catalogo geral.json"
    if not selection_path.is_file() or not catalog_path.is_file():
        raise SystemExit("External unique-media attestation files are missing")

    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

    selected = int(
        selection.get("downloaded_unique_assets")
        or selection.get("selected_unique_assets")
        or 0
    )
    required = int(selection.get("required_unique_assets") or 0)
    rows = [
        row
        for row in (selection.get("segment_asset_map") or [])
        if row.get("segment_index") is not None
    ]
    ids = [str(row.get("asset_id")) for row in rows if row.get("asset_id")]
    segments = selection.get("segments") or []

    errors: list[str] = []
    if selection.get("reuse_allowed") is not False:
        errors.append("selection manifest does not enforce reuse_allowed=false")
    if required < 1 or selected < required:
        errors.append(f"insufficient unique media: selected={selected}, required={required}")
    if not ids or len(ids) != len(set(ids)):
        errors.append("semantic media map contains duplicate or missing asset IDs")
    if segments and len(ids) != len(segments):
        errors.append(f"semantic mapping incomplete: mapped={len(ids)}, segments={len(segments)}")

    catalog_assets = catalog.get("assets") or []
    local_names = [str(item.get("local_name") or "") for item in catalog_assets]
    if len(catalog_assets) != selected:
        errors.append(f"catalog count mismatch: catalog={len(catalog_assets)}, selected={selected}")
    if not local_names or any(not name for name in local_names):
        errors.append("download catalog contains empty local_name values")
    if len(local_names) != len(set(local_names)):
        errors.append("download catalog repeats a local media file")

    missing_files = [name for name in local_names if not (root / name).is_file()]
    if missing_files:
        errors.append(f"downloaded media files missing: {missing_files[:5]}")

    if errors:
        raise SystemExit(
            "External unique-media verification failed before TTS/render: " + "; ".join(errors)
        )

    result: dict[str, int | bool | str] = {
        "verified": True,
        "mode": "external_selection_manifest",
        "selected_unique_assets": selected,
        "required_unique_assets": required,
        "mapped_segments": len(ids),
        "local_files_verified": len(local_names),
        "reuse_allowed": False,
    }
    print(json.dumps({"external_unique_media": result}, ensure_ascii=False, sort_keys=True))
    return result


def verify_runtime_hardening_v2(stats: dict[str, int]) -> None:
    strict_unique = legacy.enabled("MEDIAFORGE_STRICT_UNIQUE_MEDIA", True)
    strict_tts = legacy.enabled("MEDIAFORGE_TTS_STRICT_CHUNK_ORDER", True)
    errors: list[str] = []
    external_media: dict[str, int | bool | str] | None = None

    if stats["python_sources"] < 1 or stats["syntax_verified_sources"] != stats["python_sources"]:
        errors.append("not every private Python source passed post-hardening syntax validation")

    if strict_unique:
        internal_verified = (
            stats["unique_media_sources"] >= 1
            and stats["unique_media_patches"] >= 1
        )
        if not internal_verified:
            # Newer v7 cores may not expose the legacy use_counts/relative_path selector.
            # In that case we only proceed after independently re-validating the exact
            # downloaded semantic selection manifest and local files used by this run.
            external_media = verify_external_unique_media()

    if strict_tts and stats["tts_sources_seen"] < 1:
        errors.append("no private TTS/chunk source was found for ordered-audio verification")

    payload = {
        "runtime_hardening": {
            **stats,
            "strict_unique_media": strict_unique,
            "strict_tts_chunk_order": strict_tts,
            "unique_media_verification": (
                "private_core_guard"
                if stats["unique_media_sources"] >= 1 and stats["unique_media_patches"] >= 1
                else "external_selection_manifest"
            ),
            "external_unique_media": external_media,
            "ordered_future_guard": strict_tts,
            "natural_filesystem_order_guard": strict_tts,
            "verified": not errors,
        }
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if errors:
        raise SystemExit(
            "Runtime hardening verification failed before TTS/render: " + "; ".join(errors)
        )


legacy.verify_runtime_hardening = verify_runtime_hardening_v2

if __name__ == "__main__":
    legacy.main()
