from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.http import MediaFileUpload

from youtube_hosted_common import assert_channel, materialize_uri, sb_get, sb_patch, verify_video, youtube_client

RENDER_VERSION = "leonidanos-factory-v1"
MAX_ATTEMPTS = 5


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_job(queue_id: str, locale: str) -> dict:
    rows = sb_get("youtube_factory_jobs", {
        "select": "*", "queue_id": f"eq.{queue_id}", "locale": f"eq.{locale}",
        "status": "eq.completed", "order": "completed_at.desc", "limit": "1",
    })
    if not rows:
        raise RuntimeError(f"Completed factory job not found queue={queue_id} locale={locale}")
    job = rows[0]
    result = dict((job.get("metadata") or {}).get("result") or {})
    if not str(result.get("render_version") or "").startswith(RENDER_VERSION):
        raise RuntimeError("Safety block: Shorts do not belong to the clean factory render contract")
    shorts = [str(x) for x in result.get("shorts") or [] if str(x).strip()]
    if len(shorts) != 5:
        raise RuntimeError(f"Expected exactly five Shorts, found {len(shorts)}")
    return job


def fetch_variant(queue_id: str, locale: str) -> dict:
    rows = sb_get("youtube_video_variants", {
        "select": "id,queue_id,locale,status,youtube_title,youtube_description,youtube_privacy_status,youtube_video_id,render_version",
        "queue_id": f"eq.{queue_id}", "locale": f"eq.{locale}", "limit": "1",
    })
    if not rows:
        raise RuntimeError("Variant not found")
    row = rows[0]
    if row.get("status") != "uploaded" or not row.get("youtube_video_id"):
        raise RuntimeError("Long-form upload must be checkpointed before Shorts")
    if row.get("youtube_privacy_status") != "private":
        raise RuntimeError("Safety block: variant is not PRIVATE")
    if not str(row.get("render_version") or "").startswith(RENDER_VERSION):
        raise RuntimeError("Safety block: variant render contract mismatch")
    return row


def probe_short(path: Path) -> None:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path),
    ], capture_output=True, text=True, timeout=60, check=True)
    data = json.loads(result.stdout)
    stream = (data.get("streams") or [{}])[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    duration = float((data.get("format") or {}).get("duration") or 0)
    if width <= 0 or height <= 0 or width >= height:
        raise RuntimeError(f"Invalid Shorts geometry: {width}x{height}")
    if duration <= 0 or duration > 180.5:
        raise RuntimeError(f"Invalid Shorts duration: {duration:.2f}s")


def persist_state(job: dict, state: dict) -> None:
    metadata = dict(job.get("metadata") or {})
    metadata["youtube_shorts_upload"] = state
    updated = sb_patch("youtube_factory_jobs", {"id": f"eq.{job['id']}"}, {"metadata": metadata, "updated_at": now_iso()})
    if not updated:
        raise RuntimeError("Could not persist Shorts upload checkpoint")
    job["metadata"] = metadata


def upload_short(youtube, path: Path, title: str, description: str) -> str:
    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "categoryId": "20",
        },
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=False)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"SHORT_UPLOAD_PROGRESS {int(status.progress() * 100)}%", flush=True)
    video_id = str(response.get("id") or "")
    if not video_id:
        raise RuntimeError("Short upload completed without video id")
    return video_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--locale", required=True, choices=["pt-BR", "en-US"])
    parser.add_argument("--queue-id", required=True)
    args = parser.parse_args()

    job = fetch_job(args.queue_id, args.locale)
    variant = fetch_variant(args.queue_id, args.locale)
    metadata = dict(job.get("metadata") or {})
    result = dict(metadata.get("result") or {})
    uris = [str(x) for x in result["shorts"]]
    old_state = dict(metadata.get("youtube_shorts_upload") or {})
    if old_state.get("status") == "completed" and len(old_state.get("video_ids") or []) == 5:
        print(f"SHORTS_ALREADY_COMPLETE queue={args.queue_id} locale={args.locale}")
        return
    attempt = int(old_state.get("attempt") or 0) + 1
    if attempt > MAX_ATTEMPTS:
        raise RuntimeError("Shorts upload retry limit reached")

    ids = list(old_state.get("video_ids") or [])
    while len(ids) < 5:
        ids.append("")
    state = {"status": "running", "attempt": attempt, "video_ids": ids, "updated_at": now_iso()}
    persist_state(job, state)

    youtube = youtube_client(args.locale)
    channel_id = assert_channel(youtube, args.locale)
    base_title = str(variant.get("youtube_title") or "GTA 6")
    base_description = str(variant.get("youtube_description") or "")

    try:
        with tempfile.TemporaryDirectory(prefix="leonidanos-shorts-") as temp_dir:
            temp = Path(temp_dir)
            for index, uri in enumerate(uris):
                existing = str(ids[index] or "").strip()
                if existing:
                    verify_video(youtube, existing, channel_id, require_private=True)
                    print(f"SHORT_RECONCILED index={index + 1} video_id={existing}")
                    continue
                path = materialize_uri(uri, temp / f"short-{index + 1:02d}.mp4")
                probe_short(path)
                title = f"{base_title} #{index + 1} #Shorts"
                description = (base_description + "\n\n#GTA6 #Shorts").strip()
                video_id = upload_short(youtube, path, title, description)
                verify_video(youtube, video_id, channel_id, require_private=True)
                ids[index] = video_id
                state.update({"video_ids": ids, "updated_at": now_iso()})
                persist_state(job, state)
                print(f"SHORT_UPLOAD_OK index={index + 1} video_id={video_id} privacy=private")
        state.update({"status": "completed", "video_ids": ids, "completed_at": now_iso(), "updated_at": now_iso()})
        persist_state(job, state)
        print(f"SHORTS_COMPLETE queue={args.queue_id} locale={args.locale} count=5 privacy=private")
    except Exception as exc:
        state.update({"status": "failed", "last_error": str(exc)[:1800], "updated_at": now_iso()})
        persist_state(job, state)
        raise


if __name__ == "__main__":
    main()
