from __future__ import annotations

import argparse
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.http import MediaFileUpload

from youtube_hosted_common import (
    assert_channel,
    materialize_uri,
    sb_get,
    sb_patch,
    verify_video,
    youtube_client,
)

RENDER_VERSION = "leonidanos-factory-v1"
MAX_ATTEMPTS = 5


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_variant(queue_id: str, locale: str) -> dict:
    rows = sb_get(
        "youtube_video_variants",
        {
            "select": "*",
            "queue_id": f"eq.{queue_id}",
            "locale": f"eq.{locale}",
            "limit": "1",
        },
    )
    if not rows:
        raise RuntimeError(f"Variant not found queue={queue_id} locale={locale}")
    row = rows[0]
    if row.get("youtube_privacy_status") != "private":
        raise RuntimeError("Safety block: variant privacy is not PRIVATE")
    if not str(row.get("render_version") or "").startswith(RENDER_VERSION):
        raise RuntimeError(f"Safety block: untrusted render version {row.get('render_version')}")
    if not row.get("video_url"):
        raise RuntimeError("Variant has no rendered video")
    if not row.get("thumbnail_url"):
        raise RuntimeError("Variant has no thumbnail")
    return row


def claim(row: dict) -> dict | None:
    attempts = int(row.get("attempts") or 0)
    if attempts >= MAX_ATTEMPTS:
        raise RuntimeError(f"Upload retry limit reached: {attempts}")
    status = str(row.get("status") or "")
    if status not in {"upload_ready", "failed", "video_ready", "thumbnail_ready"}:
        raise RuntimeError(f"Variant is not uploadable from status={status}")
    params = {"id": f"eq.{row['id']}", "status": f"eq.{status}", "youtube_video_id": "is.null"}
    updated = sb_patch(
        "youtube_video_variants",
        params,
        {"status": "uploading", "attempts": attempts + 1, "last_error": None, "updated_at": now_iso()},
    )
    return updated[0] if updated else None


def checkpoint_video(row: dict, video_id: str) -> None:
    updated = sb_patch(
        "youtube_video_variants",
        {"id": f"eq.{row['id']}"},
        {"youtube_video_id": video_id, "status": "uploading", "last_error": None, "updated_at": now_iso()},
    )
    if not updated:
        raise RuntimeError("Could not persist YouTube video checkpoint")


def mark_uploaded(row: dict, video_id: str, locale: str) -> None:
    updated = sb_patch(
        "youtube_video_variants",
        {"id": f"eq.{row['id']}"},
        {"youtube_video_id": video_id, "status": "uploaded", "last_error": None, "updated_at": now_iso()},
    )
    if not updated:
        raise RuntimeError("Could not mark YouTube variant uploaded")
    if locale == "pt-BR":
        sb_patch(
            "youtube_queue",
            {"id": f"eq.{row['queue_id']}"},
            {"youtube_video_id": video_id, "last_error": None, "updated_at": now_iso()},
        )


def mark_failed(row: dict, error: str) -> None:
    sb_patch(
        "youtube_video_variants",
        {"id": f"eq.{row['id']}"},
        {"status": "failed", "last_error": error[:1800], "updated_at": now_iso()},
    )


def upload_video(youtube, row: dict, video_path: Path) -> str:
    title = str(row.get("youtube_title") or "Leonidanos")[:100]
    description = str(row.get("youtube_description") or "")
    body = {
        "snippet": {"title": title, "description": description, "categoryId": "20"},
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=False)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"UPLOAD_PROGRESS {int(status.progress() * 100)}%", flush=True)
    video_id = str(response.get("id") or "")
    if not video_id:
        raise RuntimeError("YouTube upload completed without video id")
    return video_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--locale", required=True, choices=["pt-BR", "en-US"])
    parser.add_argument("--queue-id", required=True)
    args = parser.parse_args()

    row = fetch_variant(args.queue_id, args.locale)
    youtube = youtube_client(args.locale)
    channel_id = assert_channel(youtube, args.locale)

    checkpoint = str(row.get("youtube_video_id") or "").strip()
    with tempfile.TemporaryDirectory(prefix="leonidanos-youtube-") as temp_dir:
        temp = Path(temp_dir)
        thumbnail_path = materialize_uri(str(row["thumbnail_url"]), temp / "thumbnail.png")

        if checkpoint:
            verify_video(youtube, checkpoint, channel_id, require_private=True)
            youtube.thumbnails().set(
                videoId=checkpoint,
                media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/png", resumable=False),
            ).execute()
            mark_uploaded(row, checkpoint, args.locale)
            print(f"UPLOAD_RECONCILED locale={args.locale} queue={args.queue_id} video_id={checkpoint}")
            return

        claimed = claim(row)
        if not claimed:
            print(f"UPLOAD_SKIPPED queue={args.queue_id} locale={args.locale} reason=concurrent_claim")
            return
        row = claimed
        try:
            video_path = materialize_uri(str(row["video_url"]), temp / "long-form.mp4")
            video_id = upload_video(youtube, row, video_path)
            checkpoint_video(row, video_id)
            verify_video(youtube, video_id, channel_id, require_private=True)
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/png", resumable=False),
            ).execute()
            verify_video(youtube, video_id, channel_id, require_private=True)
            mark_uploaded(row, video_id, args.locale)
            print(f"UPLOAD_OK locale={args.locale} queue={args.queue_id} video_id={video_id} privacy=private")
        except Exception as exc:
            mark_failed(row, str(exc))
            raise


if __name__ == "__main__":
    main()
