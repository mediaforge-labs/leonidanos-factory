from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

DIRECT_PREFIX = "supabase://"
CHUNKED_PREFIX = "supabase-chunked://"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube"]


def supabase_key() -> str:
    value = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SECRET_KEY") or "").strip()
    if not value:
        raise RuntimeError("Missing Supabase service key")
    return value


def supabase_url() -> str:
    value = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not value:
        raise RuntimeError("Missing SUPABASE_URL")
    return value


def sb_headers(content_type: str = "application/json") -> dict[str, str]:
    key = supabase_key()
    headers = {"apikey": key, "Content-Type": content_type}
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def sb_get(table: str, params: dict[str, str]) -> list[dict]:
    response = requests.get(f"{supabase_url()}/rest/v1/{table}", headers=sb_headers(), params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def sb_patch(table: str, params: dict[str, str], body: dict) -> list[dict]:
    headers = sb_headers()
    headers["Prefer"] = "return=representation"
    response = requests.patch(
        f"{supabase_url()}/rest/v1/{table}", headers=headers, params=params, json=body, timeout=30
    )
    response.raise_for_status()
    return response.json() if response.content else []


def storage_url(bucket: str, object_path: str) -> str:
    return f"{supabase_url()}/storage/v1/object/{bucket}/{quote(object_path, safe='/')}"


def parse_uri(uri: str, prefix: str) -> tuple[str, str]:
    value = uri[len(prefix):]
    if "/" not in value:
        raise RuntimeError(f"Invalid storage URI: {uri}")
    bucket, object_path = value.split("/", 1)
    return bucket, object_path


def fetch_storage_bytes(bucket: str, object_path: str) -> bytes:
    response = requests.get(storage_url(bucket, object_path), headers=sb_headers("application/octet-stream"), timeout=(30, 600))
    response.raise_for_status()
    return response.content


def materialize_uri(uri: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if uri.startswith(DIRECT_PREFIX):
        bucket, object_path = parse_uri(uri, DIRECT_PREFIX)
        destination.write_bytes(fetch_storage_bytes(bucket, object_path))
    elif uri.startswith(CHUNKED_PREFIX):
        bucket, manifest_path = parse_uri(uri, CHUNKED_PREFIX)
        manifest = json.loads(fetch_storage_bytes(bucket, manifest_path).decode("utf-8"))
        expected_total = int(manifest.get("total_size") or 0)
        expected_sha = str(manifest.get("sha256") or "")
        total_hash = hashlib.sha256()
        written = 0
        with destination.open("wb") as out:
            for item in sorted(manifest.get("chunks") or [], key=lambda x: int(x["index"])):
                data = fetch_storage_bytes(bucket, str(item["object_path"]))
                if len(data) != int(item["size"]):
                    raise RuntimeError(f"Chunk size mismatch: {item['object_path']}")
                if hashlib.sha256(data).hexdigest() != str(item["sha256"]):
                    raise RuntimeError(f"Chunk SHA256 mismatch: {item['object_path']}")
                out.write(data)
                total_hash.update(data)
                written += len(data)
        if written != expected_total:
            raise RuntimeError(f"Chunked object size mismatch: {written} != {expected_total}")
        if expected_sha and total_hash.hexdigest() != expected_sha:
            raise RuntimeError("Chunked object total SHA256 mismatch")
    elif uri.startswith(("http://", "https://")):
        with requests.get(uri, timeout=(30, 600), stream=True) as response:
            response.raise_for_status()
            with destination.open("wb") as out:
                for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                    if chunk:
                        out.write(chunk)
    else:
        raise RuntimeError(f"Unsupported media URI: {uri}")
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError(f"Downloaded file is empty: {destination}")
    return destination


def youtube_env(locale: str) -> tuple[str, str, str, str]:
    prefix = "YOUTUBE_PT" if locale == "pt-BR" else "YOUTUBE_EN"
    values = tuple(os.environ.get(f"{prefix}_{suffix}", "").strip() for suffix in (
        "CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN", "CHANNEL_ID"
    ))
    if any(not value for value in values):
        raise RuntimeError(f"Missing YouTube credentials for {locale}")
    return values  # type: ignore[return-value]


def youtube_client(locale: str):
    client_id, client_secret, refresh_token, _ = youtube_env(locale)
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def assert_channel(youtube, locale: str) -> str:
    expected = youtube_env(locale)[3]
    response = youtube.channels().list(part="id,snippet", mine=True).execute()
    ids = [str(item.get("id") or "") for item in response.get("items") or []]
    if expected not in ids:
        raise RuntimeError(f"OAuth channel mismatch for {locale}: expected {expected}, authenticated {ids}")
    return expected


def verify_video(youtube, video_id: str, expected_channel_id: str, require_private: bool = True) -> dict:
    response = youtube.videos().list(part="id,snippet,status", id=video_id).execute()
    items = response.get("items") or []
    if not items:
        raise RuntimeError(f"YouTube checkpoint video does not exist: {video_id}")
    item = items[0]
    channel_id = str((item.get("snippet") or {}).get("channelId") or "")
    privacy = str((item.get("status") or {}).get("privacyStatus") or "")
    if channel_id != expected_channel_id:
        raise RuntimeError(f"YouTube checkpoint belongs to another channel: {channel_id}")
    if require_private and privacy != "private":
        raise RuntimeError(f"Safety block: YouTube video {video_id} is {privacy}, expected private")
    return item
