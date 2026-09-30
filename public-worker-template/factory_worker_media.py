#!/usr/bin/env python3
from __future__ import annotations

"""Clean Leonidanos production worker with Dell-as-file-supplier media preflight.

GitHub remains the render factory. The Dell gateway is used only to supply the
owner-curated GTA VI edit assets required by the encrypted MediaForge core.
"""

import argparse
from collections import deque
import json
import os
import pathlib
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import factory_worker as base

RENDER_VERSION = "leonidanos-factory-v2-media-preflight"
MAX_ASSETS = 180
MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024


def run_streamed(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    """Stream child output and retain a safe tail for Supabase failure diagnostics."""
    printable = " ".join(str(part) for part in cmd[:2])
    process = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    tail: deque[str] = deque(maxlen=80)
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        tail.append(line)
    return_code = process.wait()
    if return_code != 0:
        detail = "".join(tail).strip()
        if len(detail) > 1450:
            detail = detail[-1450:]
        raise RuntimeError(
            f"MediaForge subprocess failed (exit={return_code}, command={printable}). "
            f"Diagnostic tail:\n{detail}"
        )


def validate_media_pool(asset_root: pathlib.Path) -> dict:
    selection_path = asset_root / "mediaforge-selection.json"
    catalog_path = asset_root / "Catalogo geral.json"
    if not selection_path.is_file() or not catalog_path.is_file():
        raise RuntimeError("Dell media preflight did not produce selection/catalog manifests")

    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    selected = int(selection.get("downloaded_unique_assets") or selection.get("selected_unique_assets") or 0)
    required = int(selection.get("required_unique_assets") or 0)
    rows = [
        row for row in (selection.get("segment_asset_map") or [])
        if isinstance(row, dict) and row.get("segment_index") is not None
    ]
    ids = [str(row.get("asset_id") or "").strip() for row in rows]
    ids = [value for value in ids if value]
    segments = selection.get("segments") or []

    if selection.get("reuse_allowed") is not False:
        raise RuntimeError("Media pool does not enforce reuse_allowed=false")
    if selection.get("scope") != "gta-vi-owner-curated-only":
        raise RuntimeError(f"Media pool scope is invalid: {selection.get('scope')!r}")
    if required < 1 or selected < required:
        raise RuntimeError(f"Insufficient unique media: selected={selected}, required={required}")
    if not ids or len(ids) != len(set(ids)):
        raise RuntimeError("Duplicate or missing assets detected in semantic media map")
    if segments and len(ids) != len(segments):
        raise RuntimeError(f"Incomplete semantic mapping: mapped={len(ids)}, segments={len(segments)}")

    assets = [row for row in (catalog.get("assets") or []) if isinstance(row, dict)]
    if len(assets) != selected:
        raise RuntimeError(f"Catalog/file count mismatch: catalog={len(assets)}, selected={selected}")
    local_names = [str(row.get("local_name") or "").strip() for row in assets]
    if not local_names or len(local_names) != len(set(local_names)):
        raise RuntimeError("Downloaded media catalog has empty or duplicate local filenames")

    for name in local_names:
        path = (asset_root / name).resolve()
        try:
            path.relative_to(asset_root.resolve())
        except ValueError as exc:
            raise RuntimeError(f"Unsafe downloaded media path: {name}") from exc
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"Invalid downloaded media: {path}")
        subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height", "-of", "json", str(path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )

    return {
        "selected": selected,
        "required": required,
        "mapped_segments": len(ids),
        "stale_catalog_assets_skipped": len(selection.get("unavailable_catalog_assets") or []),
    }


def build_manifest(job: dict, queue: dict, variant: dict, lane: str, locale: str) -> dict:
    manifest = base.build_job_manifest(job, queue, variant, lane, locale)
    metadata = manifest["jobs"][lane]["metadata"]
    metadata.update({
        "music_enabled": False,
        "video_library_enabled": True,
        "video_library_max_assets": MAX_ASSETS,
        "unique_media_only": True,
        "tts_chunk_order_strict": True,
        "factory_repository": "mediaforge-labs/leonidanos-factory",
    })
    return manifest


def run_factory(args: argparse.Namespace) -> int:
    owner = f"github:{os.environ.get('GITHUB_RUN_ID', 'local')}:{args.lane}"
    job = None
    variant = None
    asset_root: pathlib.Path | None = None
    queue_before_status = ""

    try:
        job = base.lease_job(args.locale, owner)
        if not job:
            print(json.dumps({"status": "idle", "lane": args.lane, "locale": args.locale}))
            return 0

        queue = base.fetch_one("youtube_queue", id=job["queue_id"])
        if not queue:
            raise RuntimeError(f"Queue row not found: {job['queue_id']}")
        queue_before_status = str(queue.get("status") or "")

        variant_id = (job.get("metadata") or {}).get("variant_id")
        if variant_id:
            variant = base.fetch_one("youtube_video_variants", id=variant_id)
        if not variant:
            variant = base.fetch_one("youtube_video_variants", queue_id=job["queue_id"], locale=args.locale)
        if not variant:
            raise RuntimeError(f"Video variant not found for queue={job['queue_id']} locale={args.locale}")
        if variant.get("youtube_privacy_status") != "private":
            raise RuntimeError("Safety block: factory variant is not PRIVATE")
        if variant.get("youtube_video_id"):
            raise RuntimeError("Safety block: variant already has a YouTube checkpoint; refusing rerender")

        if args.locale == "pt-BR":
            base.patch_rows("youtube_queue", {"status": "rendering", "last_error": None, "updated_at": base.utcnow()}, id=job["queue_id"])

        runtime = pathlib.Path(args.runtime_dir)
        runtime.mkdir(parents=True, exist_ok=True)
        manifest = build_manifest(job, queue, variant, args.lane, args.locale)
        job_path = runtime / f"factory-job-{args.lane}.json"
        script_path = runtime / f"factory-script-{args.lane}.txt"
        job_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        lane_job = manifest["jobs"][args.lane]
        script_path.write_text(str(lane_job["script"]), encoding="utf-8")

        asset_root = runtime / f"factory-assets-{args.lane}"
        run_streamed([
            sys.executable,
            args.asset_client,
            "--title", str(lane_job["title"]),
            "--script-file", str(script_path),
            "--dest", str(asset_root),
            "--max-assets", str(MAX_ASSETS),
            "--max-total-bytes", str(MAX_TOTAL_BYTES),
            "--job-key", f"factory-{os.environ.get('GITHUB_RUN_ID', 'local')}-{args.lane}",
        ], env=os.environ.copy())

        media_result = validate_media_pool(asset_root)
        print(json.dumps({"status": "media_preflight_ok", **media_result}, ensure_ascii=False))

        env = os.environ.copy()
        env["MEDIAFORGE_TTS_PROVIDER"] = "chatterbox"
        env["MEDIAFORGE_TTS_MAX_WORKERS"] = "1"
        env["MEDIAFORGE_TTS_STRICT_CHUNK_ORDER"] = "1"
        env["MEDIAFORGE_STRICT_UNIQUE_MEDIA"] = "1"
        env["MEDIAFORGE_DEBUG_SUBPROCESS_STDERR"] = "1"
        env["MEDIAFORGE_VIDEO_LIBRARY_ROOT"] = str(asset_root.resolve())
        env["MEDIAFORGE_MUSIC_ENABLED"] = "0"
        env["SUPABASE_SECRET_KEY"] = base.service_key()

        worker_cmd = [
            sys.executable, args.worker,
            "--bundle", args.bundle,
            "--job", str(job_path),
            "--lane", args.lane,
            "--locale", args.locale,
            "--output-dir", args.output_dir,
        ]
        run_streamed([*worker_cmd, "--verify-only"], env=env)
        run_streamed(worker_cmd, env=env)

        out = pathlib.Path(args.output_dir)
        manifest_out, video_path, audio_path = base.validate_outputs(out)
        run_streamed([
            sys.executable, args.validator,
            "--root", str(out),
            "--selection", str(asset_root / "mediaforge-selection.json"),
            "--require-five-shorts",
        ], env=env)

        run_key = os.environ.get("GITHUB_RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
        prefix = f"renders/{job['queue_id']}/{args.locale}/{run_key}"
        video_uri = base.durable_upload(video_path, f"{prefix}/long-form.mp4", "video/mp4")
        audio_uri = base.durable_upload(audio_path, f"{prefix}/narration.wav", "audio/wav")
        manifest_uri = base.durable_upload(out / "manifest.json", f"{prefix}/manifest.json", "application/json")
        selection_uri = base.durable_upload(asset_root / "mediaforge-selection.json", f"{prefix}/mediaforge-selection.json", "application/json")
        base.durable_upload(out / "captions" / "long-form.srt", f"{prefix}/long-form.srt", "application/x-subrip")

        short_uris = [
            base.durable_upload(short, f"{prefix}/shorts/{short.name}", "video/mp4")
            for short in sorted((out / "shorts").glob("short-*.mp4"))
        ]
        expected_shorts = int((job.get("metadata") or {}).get("shorts_requested") or 5)
        if len(short_uris) != expected_shorts:
            raise RuntimeError(f"Expected {expected_shorts} Shorts but render produced {len(short_uris)}")

        current = str(variant.get("status") or "")
        next_status = "upload_ready" if current == "thumbnail_ready" else "video_ready"
        if current in {"uploaded", "uploading"}:
            next_status = current
        duration = float((manifest_out.get("metrics") or {}).get("narration_seconds") or 0)
        base.patch_rows("youtube_video_variants", {
            "status": next_status,
            "audio_url": audio_uri,
            "video_url": video_uri,
            "video_duration_seconds": duration,
            "render_version": RENDER_VERSION,
            "last_error": None,
            "updated_at": base.utcnow(),
        }, id=variant["id"])

        if args.locale == "pt-BR":
            restore_status = queue_before_status
            if restore_status in {"", "failed", "rendering"}:
                restore_status = "voice_ready"
            base.patch_rows("youtube_queue", {
                "status": restore_status,
                "audio_url": audio_uri,
                "video_url": video_uri,
                "last_error": None,
                "updated_at": base.utcnow(),
            }, id=job["queue_id"])

        metadata = dict(job.get("metadata") or {})
        metadata["result"] = {
            "video_url": video_uri,
            "audio_url": audio_uri,
            "manifest_url": manifest_uri,
            "selection_url": selection_uri,
            "shorts": short_uris,
            "render_version": RENDER_VERSION,
            "metrics": manifest_out.get("metrics") or {},
            "media_preflight": media_result,
            "music_enabled": False,
        }
        base.patch_rows("youtube_factory_jobs", {
            "status": "completed",
            "fallback_reason": None,
            "last_progress_at": base.utcnow(),
            "completed_at": base.utcnow(),
            "lease_expires_at": None,
            "lease_owner": None,
            "metadata": metadata,
            "updated_at": base.utcnow(),
        }, id=job["id"])

        print(json.dumps({
            "status": "completed",
            "job_id": job["id"],
            "lane": args.lane,
            "locale": args.locale,
            "video_url": video_uri,
            "shorts": len(short_uris),
            "render_version": RENDER_VERSION,
        }, ensure_ascii=False))
        return 0

    except Exception as exc:
        base.mark_failed(job, variant, owner, str(exc))
        raise
    finally:
        if asset_root is not None:
            shutil.rmtree(asset_root, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", required=True)
    ap.add_argument("--locale", required=True)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--worker", default="public-worker-template/worker.py")
    ap.add_argument("--asset-client", default="public-worker-template/dell_asset_client_v7.py")
    ap.add_argument("--validator", default="public-worker-template/validate_render.py")
    ap.add_argument("--runtime-dir", default=".runtime")
    ap.add_argument("--output-dir", required=True)
    args = ap.parse_args()
    raise SystemExit(run_factory(args))


if __name__ == "__main__":
    main()
