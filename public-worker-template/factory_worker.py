#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import requests

from supabase_chunked import upload_direct_file, upload_file

SUPABASE_BUCKET = "mediaforge-assets"
RENDER_VERSION = "leonidanos-factory-v1"
CHUNK_THRESHOLD_BYTES = 45 * 1024 * 1024


def service_key() -> str:
    value = (os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not value:
        raise RuntimeError("Missing Supabase service key")
    return value


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def api_headers() -> dict[str, str]:
    key = service_key()
    headers = {"apikey": key, "User-Agent": "LeonidanosFactory/1.0"}
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def rest_url(path: str) -> str:
    return f"{required('SUPABASE_URL').rstrip('/')}/rest/v1/{path.lstrip('/')}"


def request_json(method: str, url: str, *, params=None, body=None, extra_headers=None, timeout=60):
    headers = api_headers()
    headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    response = requests.request(method, url, headers=headers, params=params, json=body, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"Supabase request failed ({response.status_code}): {response.text[:500]}")
    return response.json() if response.content else None


def lease_job(locale: str, owner: str) -> dict | None:
    payload = request_json(
        "POST",
        rest_url("rpc/lease_mediaforge_job"),
        body={"p_locale": locale, "p_owner": owner, "p_lease_minutes": 180},
        extra_headers={"Prefer": "return=representation"},
    ) or []
    return payload[0] if payload else None


def fetch_one(table: str, **filters) -> dict | None:
    params = {"select": "*", "limit": "1"}
    for key, value in filters.items():
        params[key] = f"eq.{value}"
    rows = request_json("GET", rest_url(table), params=params) or []
    return rows[0] if rows else None


def patch_rows(table: str, body: dict, **filters) -> list[dict]:
    params = {key: f"eq.{value}" for key, value in filters.items()}
    rows = request_json(
        "PATCH",
        rest_url(table),
        params=params,
        body=body,
        extra_headers={"Prefer": "return=representation"},
    )
    return rows or []


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_job_manifest(job: dict, queue: dict, variant: dict, lane: str, locale: str) -> dict:
    script = (variant.get("tts_text") or variant.get("script") or queue.get("tts_text") or queue.get("script") or "").strip()
    title = (variant.get("youtube_title") or queue.get("youtube_title") or "Leonidanos").strip()
    if len(script.split()) < 20:
        raise RuntimeError(f"Factory job has no usable TTS script for {locale}")
    metadata = dict(job.get("metadata") or {})
    metadata.setdefault("music_enabled", False)
    metadata.setdefault("video_library_enabled", True)
    metadata.setdefault("video_library_max_assets", 18)
    metadata["queue_id"] = job.get("queue_id")
    metadata["variant_id"] = variant.get("id")
    metadata["factory_repository"] = "mediaforge-labs/leonidanos-factory"
    return {
        "version": 1,
        "mode": "production",
        "jobs": {
            lane: {
                "id": job.get("job_key") or str(job["id"]),
                "locale": locale,
                "title": title,
                "shorts_requested": int(metadata.get("shorts_requested") or 5),
                "metadata": metadata,
                "script": script,
                "media": [],
            }
        },
    }


def durable_upload(local_path: pathlib.Path, storage_path: str, content_type: str | None = None) -> str:
    local_path = pathlib.Path(local_path)
    content_type = content_type or mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    if local_path.stat().st_size >= CHUNK_THRESHOLD_BYTES:
        return upload_file(local_path, bucket=SUPABASE_BUCKET, storage_path=storage_path, content_type=content_type)
    return upload_direct_file(local_path, bucket=SUPABASE_BUCKET, storage_path=storage_path, content_type=content_type)


def mark_failed(job: dict | None, variant: dict | None, owner: str, error: str) -> None:
    message = error[:1800]
    if job:
        metadata = dict(job.get("metadata") or {})
        metadata["last_error"] = message
        metadata["failed_at"] = utcnow()
        try:
            patch_rows(
                "youtube_factory_jobs",
                {
                    "status": "failed",
                    "fallback_reason": message,
                    "last_progress_at": utcnow(),
                    "completed_at": utcnow(),
                    "lease_expires_at": None,
                    "lease_owner": None,
                    "metadata": metadata,
                    "updated_at": utcnow(),
                },
                id=job["id"],
            )
        except Exception:
            pass
    if variant:
        try:
            patch_rows("youtube_video_variants", {"status": "failed", "last_error": message, "updated_at": utcnow()}, id=variant["id"])
        except Exception:
            pass


def validate_outputs(out: pathlib.Path) -> tuple[dict, pathlib.Path, pathlib.Path]:
    manifest_path = out / "manifest.json"
    video_path = out / "video" / "long-form.mp4"
    audio_path = out / "audio" / "narration.wav"
    captions_path = out / "captions" / "long-form.srt"
    for path in (manifest_path, video_path, audio_path, captions_path):
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"MediaForge output missing or empty: {path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    duration = float((manifest.get("metrics") or {}).get("narration_seconds") or 0)
    if duration <= 0:
        raise RuntimeError("MediaForge manifest does not contain a valid narration duration")
    return manifest, video_path, audio_path


def run_factory(args: argparse.Namespace) -> int:
    owner = f"github:{os.environ.get('GITHUB_RUN_ID','local')}:{args.lane}"
    job = None
    variant = None
    try:
        job = lease_job(args.locale, owner)
        if not job:
            print(json.dumps({"status": "idle", "lane": args.lane, "locale": args.locale}))
            return 0

        queue = fetch_one("youtube_queue", id=job["queue_id"])
        if not queue:
            raise RuntimeError(f"Queue row not found: {job['queue_id']}")
        variant_id = (job.get("metadata") or {}).get("variant_id")
        if variant_id:
            variant = fetch_one("youtube_video_variants", id=variant_id)
        if not variant:
            variant = fetch_one("youtube_video_variants", queue_id=job["queue_id"], locale=args.locale)
        if not variant:
            raise RuntimeError(f"Video variant not found for queue={job['queue_id']} locale={args.locale}")
        if variant.get("youtube_privacy_status") != "private":
            raise RuntimeError("Safety block: factory variant is not PRIVATE")

        patch_rows("youtube_queue", {"status": "rendering", "updated_at": utcnow()}, id=job["queue_id"])
        runtime = pathlib.Path(args.runtime_dir)
        runtime.mkdir(parents=True, exist_ok=True)
        job_path = runtime / f"factory-job-{args.lane}.json"
        job_path.write_text(json.dumps(build_job_manifest(job, queue, variant, args.lane, args.locale), ensure_ascii=False, indent=2), encoding="utf-8")

        env = os.environ.copy()
        env["MEDIAFORGE_TTS_PROVIDER"] = "chatterbox"
        env["SUPABASE_SECRET_KEY"] = service_key()
        subprocess.run([
            sys.executable, args.worker,
            "--bundle", args.bundle,
            "--job", str(job_path),
            "--lane", args.lane,
            "--locale", args.locale,
            "--output-dir", args.output_dir,
        ], check=True, env=env)

        out = pathlib.Path(args.output_dir)
        manifest, video_path, audio_path = validate_outputs(out)
        run_key = os.environ.get("GITHUB_RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
        prefix = f"renders/{job['queue_id']}/{args.locale}/{run_key}"

        video_uri = durable_upload(video_path, f"{prefix}/long-form.mp4", "video/mp4")
        audio_uri = durable_upload(audio_path, f"{prefix}/narration.wav", "audio/wav")
        manifest_uri = durable_upload(out / "manifest.json", f"{prefix}/manifest.json", "application/json")
        durable_upload(out / "captions" / "long-form.srt", f"{prefix}/long-form.srt", "application/x-subrip")
        short_uris = []
        for short in sorted((out / "shorts").glob("short-*.mp4")):
            short_uris.append(durable_upload(short, f"{prefix}/shorts/{short.name}", "video/mp4"))

        expected_shorts = int((job.get("metadata") or {}).get("shorts_requested") or 5)
        if len(short_uris) != expected_shorts:
            raise RuntimeError(f"Expected {expected_shorts} Shorts but render produced {len(short_uris)}")

        current = str(variant.get("status") or "")
        next_status = "upload_ready" if current == "thumbnail_ready" else "video_ready"
        if current in {"uploaded", "uploading"}:
            next_status = current
        duration = float((manifest.get("metrics") or {}).get("narration_seconds") or 0)
        patch_rows(
            "youtube_video_variants",
            {
                "status": next_status,
                "audio_url": audio_uri,
                "video_url": video_uri,
                "video_duration_seconds": duration,
                "render_version": RENDER_VERSION,
                "last_error": None,
                "updated_at": utcnow(),
            },
            id=variant["id"],
        )
        if args.locale == "pt-BR":
            patch_rows("youtube_queue", {"audio_url": audio_uri, "video_url": video_uri, "last_error": None, "updated_at": utcnow()}, id=job["queue_id"])

        metadata = dict(job.get("metadata") or {})
        metadata["result"] = {
            "video_url": video_uri,
            "audio_url": audio_uri,
            "manifest_url": manifest_uri,
            "shorts": short_uris,
            "render_version": RENDER_VERSION,
            "metrics": manifest.get("metrics") or {},
        }
        patch_rows(
            "youtube_factory_jobs",
            {
                "status": "completed",
                "last_progress_at": utcnow(),
                "completed_at": utcnow(),
                "lease_expires_at": None,
                "lease_owner": None,
                "metadata": metadata,
                "updated_at": utcnow(),
            },
            id=job["id"],
        )
        print(json.dumps({"status": "completed", "job_id": job["id"], "lane": args.lane, "video_url": video_uri}))
        return 0
    except Exception as exc:
        mark_failed(job, variant, owner, str(exc))
        raise


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", required=True)
    ap.add_argument("--locale", required=True)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--worker", default="public-worker-template/worker.py")
    ap.add_argument("--runtime-dir", default=".runtime")
    ap.add_argument("--output-dir", required=True)
    args = ap.parse_args()
    raise SystemExit(run_factory(args))


if __name__ == "__main__":
    main()
