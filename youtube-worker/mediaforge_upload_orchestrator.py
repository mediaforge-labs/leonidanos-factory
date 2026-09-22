from __future__ import annotations

import argparse
import json
import os

import requests

BASE = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip()
H = {"apikey": KEY, "Content-Type": "application/json"}
if not KEY.startswith("sb_secret_"):
    H["Authorization"] = f"Bearer {KEY}"

CANONICAL_RENDER_PREFIX = "mediaforge-github-v10-strict-watermarked-shorts-v2"
LEGACY_APPROVED_RENDER_PREFIX = "mediaforge-github-v10-approved-watermark-chunked"
DIRECT_PREFIX = "supabase://"
CHUNKED_PREFIX = "supabase-chunked://"
MAX_LONGFORM_UPLOAD_ATTEMPTS = 5
MAX_SHORTS_UPLOAD_ATTEMPTS = 5


def get(table, params):
    r = requests.get(f"{BASE}/rest/v1/{table}", headers=H, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def scoped(params: dict[str, str], queue_id: str) -> dict[str, str]:
    out = dict(params)
    if queue_id:
        out["queue_id"] = f"eq.{queue_id}"
    return out


def _hosted(uri: str) -> bool:
    return str(uri or "").startswith((DIRECT_PREFIX, CHUNKED_PREFIX))


def _canonical(row):
    return (
        _hosted(str(row.get("video_url") or ""))
        and str(row.get("render_version") or "").startswith(CANONICAL_RENDER_PREFIX)
        and row.get("youtube_privacy_status") == "private"
        and row.get("locale") in {"pt-BR", "en-US"}
    )


def _approved_reconciliation(row):
    version = str(row.get("render_version") or "")
    return (
        _hosted(str(row.get("video_url") or ""))
        and (version.startswith(CANONICAL_RENDER_PREFIX) or version.startswith(LEGACY_APPROVED_RENDER_PREFIX))
        and row.get("youtube_privacy_status") == "private"
        and row.get("locale") in {"pt-BR", "en-US"}
    )


def needs_thumbnail(queue_id: str = ""):
    rows = get("youtube_video_variants", scoped({
        "select": "id,queue_id,locale,channel_key,status,video_url,thumbnail_url,render_version,youtube_video_id,youtube_privacy_status,attempts,updated_at",
        "status": "eq.video_ready", "youtube_video_id": "is.null", "youtube_privacy_status": "eq.private",
        "order": "updated_at.asc", "limit": "20",
    }, queue_id))
    return [x for x in rows if _canonical(x) and not x.get("thumbnail_url")]


def ready(queue_id: str = ""):
    rows = get("youtube_video_variants", scoped({
        "select": "id,queue_id,locale,channel_key,status,video_url,thumbnail_url,render_version,youtube_video_id,youtube_privacy_status,attempts,updated_at",
        "status": "eq.upload_ready", "youtube_video_id": "is.null", "youtube_privacy_status": "eq.private",
        "order": "updated_at.asc", "limit": "20",
    }, queue_id))
    return [x for x in rows if _canonical(x) and x.get("thumbnail_url") and int(x.get("attempts") or 0) < MAX_LONGFORM_UPLOAD_ATTEMPTS]


def retry_failed(queue_id: str = ""):
    rows = get("youtube_video_variants", scoped({
        "select": "id,queue_id,locale,channel_key,status,video_url,thumbnail_url,render_version,youtube_video_id,youtube_privacy_status,attempts,updated_at",
        "status": "eq.failed", "youtube_video_id": "is.null", "youtube_privacy_status": "eq.private",
        "order": "updated_at.asc", "limit": "20",
    }, queue_id))
    return [x for x in rows if _canonical(x) and x.get("thumbnail_url") and int(x.get("attempts") or 0) < MAX_LONGFORM_UPLOAD_ATTEMPTS]


def resume_failed(queue_id: str = ""):
    rows = get("youtube_video_variants", scoped({
        "select": "id,queue_id,locale,channel_key,status,video_url,thumbnail_url,render_version,youtube_video_id,youtube_privacy_status,attempts,updated_at",
        "status": "in.(failed,uploading)", "youtube_privacy_status": "eq.private", "youtube_video_id": "not.is.null",
        "order": "updated_at.asc", "limit": "20",
    }, queue_id))
    return [x for x in rows if _approved_reconciliation(x) and x.get("youtube_video_id") and x.get("thumbnail_url")]


def shorts_ready(queue_id: str = ""):
    jobs = get("youtube_factory_jobs", scoped({
        "select": "id,queue_id,locale,status,metadata,completed_at,updated_at", "status": "eq.completed",
        "order": "completed_at.asc", "limit": "40",
    }, queue_id))
    eligible = []
    for job in jobs:
        locale = str(job.get("locale") or "")
        job_queue_id = str(job.get("queue_id") or "")
        if locale not in {"pt-BR", "en-US"} or not job_queue_id:
            continue
        metadata = dict(job.get("metadata") or {})
        result = dict(metadata.get("result") or {})
        render_version = str(result.get("render_version") or "")
        shorts = [str(x) for x in (result.get("shorts") or []) if str(x).strip()]
        if not render_version.startswith(CANONICAL_RENDER_PREFIX):
            continue
        if len(shorts) != 5 or any(not _hosted(x) for x in shorts):
            continue
        upload_state = dict(metadata.get("youtube_shorts_upload") or {})
        if upload_state.get("status") == "completed":
            continue
        if upload_state.get("status") == "failed" and int(upload_state.get("attempt") or 0) >= MAX_SHORTS_UPLOAD_ATTEMPTS:
            continue
        variants = get("youtube_video_variants", {
            "select": "id,queue_id,locale,channel_key,status,youtube_privacy_status,render_version,youtube_video_id",
            "queue_id": f"eq.{job_queue_id}", "locale": f"eq.{locale}", "limit": "1",
        })
        if not variants:
            continue
        variant = variants[0]
        if variant.get("youtube_privacy_status") != "private":
            continue
        if variant.get("status") != "uploaded" or not variant.get("youtube_video_id"):
            continue
        if not str(variant.get("render_version") or "").startswith(CANONICAL_RENDER_PREFIX):
            continue
        eligible.append({
            "job_id": job["id"], "queue_id": job_queue_id, "locale": locale,
            "render_version": render_version, "youtube_video_id": variant.get("youtube_video_id"),
            "shorts": shorts, "prior_upload_state": upload_state,
        })
    return eligible


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["ready", "needs-thumbnail", "retry-failed", "resume-failed", "shorts-ready"], default="ready")
    ap.add_argument("--queue-id", default="")
    args = ap.parse_args()
    queue_id = args.queue_id.strip()
    if args.mode == "needs-thumbnail": rows = needs_thumbnail(queue_id)
    elif args.mode == "retry-failed": rows = retry_failed(queue_id)
    elif args.mode == "resume-failed": rows = resume_failed(queue_id)
    elif args.mode == "shorts-ready": rows = shorts_ready(queue_id)
    else: rows = ready(queue_id)
    print(json.dumps(rows, ensure_ascii=False))
