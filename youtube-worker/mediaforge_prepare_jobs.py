from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any

import requests
from anthropic import Anthropic

import english_validation_prepare as english

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
FACTORY_VERSION = "mediaforge-v10"
MAX_AUTOMATIC_FACTORY_ATTEMPTS = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_env() -> None:
    missing = [
        name
        for name, value in {
            "SUPABASE_URL": SUPABASE_URL,
            "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY,
            "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


def headers(prefer: str | None = None) -> dict[str, str]:
    out = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": "application/json",
    }
    if not SUPABASE_SERVICE_ROLE_KEY.startswith("sb_secret_"):
        out["Authorization"] = f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"
    if prefer:
        out["Prefer"] = prefer
    return out


def sb_get(table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=headers(),
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def sb_patch(table: str, filters: dict[str, str], payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=headers("return=representation"),
        params=filters,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def upsert_variant(payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/youtube_video_variants",
        headers=headers("resolution=merge-duplicates,return=representation"),
        params={"on_conflict": "queue_id,locale"},
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        raise RuntimeError("Variant upsert returned no row")
    return rows[0]


def insert_factory_job(payload: dict[str, Any]) -> dict[str, Any] | None:
    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/youtube_factory_jobs",
        headers=headers("resolution=ignore-duplicates,return=representation"),
        params={"on_conflict": "job_key"},
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json() if response.content else []
    return rows[0] if rows else None


def select_queue(queue_id: str) -> dict[str, Any]:
    params = {
        "select": (
            "id,blog_post_id,status,script,tts_text,youtube_title,youtube_description,"
            "thumbnail_title,thumbnail_url,youtube_privacy_status,youtube_video_id,updated_at"
        ),
        "limit": "1",
    }
    if queue_id:
        params["id"] = f"eq.{queue_id}"
    else:
        params["status"] = "in.(scripted,voice_ready,failed)"
        params["tts_text"] = "not.is.null"
        params["thumbnail_url"] = "not.is.null"
        params["youtube_video_id"] = "is.null"
        params["order"] = "updated_at.desc"

    rows = sb_get("youtube_queue", params)
    if not rows:
        raise RuntimeError("No editorial queue item is ready for MediaForge preparation")
    row = rows[0]
    if row.get("youtube_privacy_status") != "private":
        raise RuntimeError(f"Safety block: queue {row['id']} is not private")
    source = str(row.get("tts_text") or row.get("script") or "").strip()
    if len(source.split()) < 20:
        raise RuntimeError(f"Queue {row['id']} has no usable narration text")
    if not row.get("thumbnail_url"):
        raise RuntimeError(f"Queue {row['id']} has no PT-BR thumbnail yet")
    row["source_text"] = source
    return row


def existing_variant(queue_id: str, locale: str) -> dict[str, Any] | None:
    rows = sb_get(
        "youtube_video_variants",
        {
            "select": "*",
            "queue_id": f"eq.{queue_id}",
            "locale": f"eq.{locale}",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def prepare_pt(row: dict[str, Any]) -> dict[str, Any]:
    existing = existing_variant(str(row["id"]), "pt-BR")
    if existing and existing.get("youtube_video_id"):
        raise RuntimeError(
            f"PT-BR variant is already checkpointed on YouTube ({existing['youtube_video_id']}); "
            "the uploader must reconcile that ID before any rerender"
        )

    return upsert_variant(
        {
            "queue_id": row["id"],
            "blog_post_id": row["blog_post_id"],
            "locale": "pt-BR",
            "channel_key": "leonidanos_pt",
            "status": "thumbnail_ready",
            "script": row.get("script"),
            "tts_text": row["source_text"],
            "youtube_title": row.get("youtube_title"),
            "youtube_description": row.get("youtube_description"),
            "thumbnail_title": row.get("thumbnail_title"),
            "thumbnail_url": row.get("thumbnail_url"),
            "youtube_privacy_status": "private",
            "last_error": None,
            "updated_at": now_iso(),
        }
    )


def prepare_en(row: dict[str, Any]) -> dict[str, Any]:
    current = existing_variant(str(row["id"]), "en-US")
    if current and current.get("youtube_video_id"):
        raise RuntimeError(
            f"EN-US variant is already checkpointed on YouTube ({current['youtube_video_id']}); "
            "the uploader must reconcile that ID before any rerender"
        )

    existing_text = str((current or {}).get("tts_text") or "").strip()
    existing_title = str((current or {}).get("youtube_title") or "").strip()
    existing_description = str((current or {}).get("youtube_description") or "").strip()
    existing_thumb_title = str((current or {}).get("thumbnail_title") or "").strip()

    if existing_text and existing_title and existing_description and existing_thumb_title:
        english_text = existing_text
        metadata = {
            "youtube_title": existing_title,
            "youtube_description": existing_description,
            "thumbnail_title": existing_thumb_title,
        }
        print("Reusing existing EN-US localization; no Anthropic call required.")
    else:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        english_text = english.generate_english(client, row["source_text"])
        metadata = english.generate_metadata(client, row, english_text)

    payload: dict[str, Any] = {
        "queue_id": row["id"],
        "blog_post_id": row["blog_post_id"],
        "locale": "en-US",
        "channel_key": "leonidanos_en",
        "status": "thumbnail_ready" if (current or {}).get("thumbnail_url") else "script_ready",
        "script": english_text,
        "tts_text": english_text,
        "youtube_title": metadata["youtube_title"],
        "youtube_description": metadata["youtube_description"],
        "thumbnail_title": metadata["thumbnail_title"],
        "youtube_privacy_status": "private",
        "last_error": None,
        "updated_at": now_iso(),
    }
    if (current or {}).get("thumbnail_url"):
        payload["thumbnail_url"] = current["thumbnail_url"]
    return upsert_variant(payload)


def ensure_factory_job(
    row: dict[str, Any],
    variant: dict[str, Any],
    locale: str,
    *,
    force_retry_failed: bool = False,
) -> dict[str, Any] | None:
    job_key = f"{FACTORY_VERSION}:{row['id']}:{locale}"
    desired_metadata = {
        "variant_id": variant["id"],
        "shorts_requested": 5,
        "music_enabled": False,
        "unique_media_only": True,
        "prepared_by": "leonidanos-factory",
        "factory_version": FACTORY_VERSION,
        "private_upload_required": True,
    }
    existing_rows = sb_get(
        "youtube_factory_jobs",
        {"select": "*", "job_key": f"eq.{job_key}", "limit": "1"},
    )
    existing = existing_rows[0] if existing_rows else None
    if not existing:
        job = insert_factory_job(
            {
                "job_key": job_key,
                "queue_id": row["id"],
                "locale": locale,
                "stage": "production",
                "preferred_backend": "github",
                "status": "pending",
                "metadata": desired_metadata,
            }
        )
        if not job:
            raise RuntimeError(f"Factory job insert raced but no row was returned: {job_key}")
        print(f"Factory job created: {job_key}")
        return job

    status = str(existing.get("status") or "")
    attempt = int(existing.get("attempt") or 0)
    if status in {"pending", "running", "completed"}:
        print(f"Factory job already {status}: {job_key}")
        return existing

    retryable_state = status in {"failed", "fallback_requested"}
    can_retry = retryable_state and (attempt < MAX_AUTOMATIC_FACTORY_ATTEMPTS or force_retry_failed)
    if not can_retry:
        raise RuntimeError(
            f"Factory job {job_key} is {status} after {attempt} attempt(s). "
            "Explicit force retry is required; refusing an uncontrolled retry loop."
        )

    metadata = dict(existing.get("metadata") or {})
    metadata.update(desired_metadata)
    metadata["requeued_at"] = now_iso()
    metadata["requeued_after_code_fix"] = bool(force_retry_failed)
    payload: dict[str, Any] = {
        "status": "pending",
        "preferred_backend": "github",
        "selected_backend": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "fallback_reason": None,
        "completed_at": None,
        "last_progress_at": now_iso(),
        "metadata": metadata,
        "updated_at": now_iso(),
    }
    if force_retry_failed and attempt >= MAX_AUTOMATIC_FACTORY_ATTEMPTS:
        payload["attempt"] = 0
    updated = sb_patch(
        "youtube_factory_jobs",
        {"id": f"eq.{existing['id']}", "status": f"eq.{status}"},
        payload,
    )
    if not updated:
        raise RuntimeError(f"Factory job changed concurrently while requeueing: {job_key}")
    print(f"Factory job requeued: {job_key} prior_status={status} prior_attempt={attempt}")
    return updated[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-id", default="")
    parser.add_argument("--force-retry-failed", action="store_true")
    args = parser.parse_args()

    require_env()
    row = select_queue(args.queue_id.strip())
    pt = prepare_pt(row)
    en = prepare_en(row)
    ensure_factory_job(row, pt, "pt-BR", force_retry_failed=args.force_retry_failed)
    ensure_factory_job(row, en, "en-US", force_retry_failed=args.force_retry_failed)

    print(
        json.dumps(
            {
                "status": "mediaforge_ready",
                "queue_id": row["id"],
                "pt_variant_id": pt["id"],
                "en_variant_id": en["id"],
                "factory_version": FACTORY_VERSION,
                "music_enabled": False,
                "shorts_requested_per_locale": 5,
                "force_retry_failed": args.force_retry_failed,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
