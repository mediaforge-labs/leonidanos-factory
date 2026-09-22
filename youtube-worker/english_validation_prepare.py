from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import requests
from anthropic import Anthropic

from youtube_voice_common import SHARD_COUNT, chunk_text, count_words

MODEL = "claude-sonnet-5"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
QUEUE_ID = os.getenv("LEONIDANOS_QUEUE_ID", "").strip()
OUTPUT_DIR = Path("youtube-worker/output/english-validation")
JOB_FILE = OUTPUT_DIR / "english-job.json"

ENGLISH_VOICE = {
    "label": "leonidanos-official-english",
    "engine": "chatterbox-multilingual-v3",
    "language": "en",
    "seed": 260918,
    "temperature": 0.55,
    "exaggeration": 1.05,
    "cfg_weight": 0.25,
    "repetition_penalty": 1.2,
    "min_p": 0.05,
    "top_p": 1.0,
    "tempo": 1.10,
    "loudnorm": "I=-16:TP=-1.5:LRA=9",
}

METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "youtube_title": {"type": "string"},
        "youtube_description": {"type": "string"},
        "thumbnail_title": {"type": "string"},
    },
    "required": ["youtube_title", "youtube_description", "thumbnail_title"],
    "additionalProperties": False,
}


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


def headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def sb_get(table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=headers(),
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def resolve_queue_id() -> str:
    if QUEUE_ID:
        return QUEUE_ID

    pt_variants = sb_get(
        "youtube_video_variants",
        {
            "select": "queue_id,status,updated_at",
            "locale": "eq.pt-BR",
            "status": "in.(video_ready,thumbnail_ready,upload_ready,uploaded)",
            "order": "updated_at.desc",
            "limit": "20",
        },
    )

    for pt in pt_variants:
        queue_id = str(pt["queue_id"])
        en_rows = sb_get(
            "youtube_video_variants",
            {
                "select": "status",
                "queue_id": f"eq.{queue_id}",
                "locale": "eq.en-US",
                "limit": "1",
            },
        )
        if not en_rows or en_rows[0].get("status") in {"planned", "script_ready", "failed"}:
            return queue_id

    raise RuntimeError("No rendered PT-BR variant is waiting for English localization")


def fetch_queue() -> dict[str, Any]:
    queue_id = resolve_queue_id()
    rows = sb_get(
        "youtube_queue",
        {
            "select": (
                "id,blog_post_id,tts_text,script,youtube_title,youtube_description,"
                "thumbnail_title,audio_url,status,updated_at"
            ),
            "id": f"eq.{queue_id}",
            "limit": "1",
        },
    )
    if not rows:
        raise RuntimeError(f"YouTube queue row not found for English localization: {queue_id}")
    row = rows[0]
    source = str(row.get("tts_text") or row.get("script") or "").strip()
    if not source:
        raise RuntimeError("Selected queue row has no Portuguese narration text")
    if not row.get("audio_url"):
        raise RuntimeError("Selected queue row has no original narration audio")
    row["source_text"] = source
    return row


def extract_text(message: Any) -> str:
    for block in message.content:
        if getattr(block, "type", None) == "text":
            return str(block.text).strip()
    raise RuntimeError("Anthropic returned no text")


def clean_text(text: str) -> str:
    text = (text or "").replace("```", "").replace("`", "")
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def generate_english(client: Anthropic, source_text: str) -> str:
    source_words = count_words(source_text)
    min_words = max(800, round(source_words * 0.92))
    max_words = max(min_words + 50, round(source_words * 1.05))

    system = (
        "You are the English-language script adapter for Leonidanos, a GTA news channel. "
        "Adapt the supplied Brazilian Portuguese narration into natural spoken US English. "
        "Preserve the exact factual meaning, order of ideas, emphasis, and overall pacing. "
        "Do not add facts, remove important facts, speculate, summarize aggressively, or change the story structure. "
        "This English audio will replace the Portuguese audio over the exact same finished video, so duration similarity is critical. "
        "Use energetic but credible gaming-news narration. Say 'GTA 6' naturally. "
        "Return only the spoken narration, with no Markdown, headings, notes, labels, or commentary."
    )

    prompt = (
        f"Source narration word count: {source_words}.\n"
        f"Target English word count: between {min_words} and {max_words} words.\n"
        "Keep paragraph flow and pacing close to the source so the resulting narration can fit the same video with only minor timing adjustment.\n\n"
        "PORTUGUESE SOURCE:\n"
        f"{source_text}"
    )

    message = client.messages.create(
        model=MODEL,
        max_tokens=6500,
        thinking={"type": "disabled"},
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    text = clean_text(extract_text(message))
    words = count_words(text)
    if words < min_words:
        raise RuntimeError(f"English narration is too short: {words} words; minimum {min_words}")
    if words > max_words:
        raise RuntimeError(f"English narration is too long: {words} words; maximum {max_words}")
    return text


def generate_metadata(client: Anthropic, row: dict[str, Any], english_text: str) -> dict[str, str]:
    system = (
        "You create English YouTube metadata for Leonidanos, a GTA news channel. "
        "Use only facts contained in the provided Portuguese metadata and English narration. "
        "Write natural US English. Do not invent facts, dates, claims, links or sources. "
        "The title must be clear and compelling without clickbait that changes the factual meaning."
    )
    payload = {
        "source_youtube_title_pt_br": row.get("youtube_title"),
        "source_youtube_description_pt_br": row.get("youtube_description"),
        "source_thumbnail_title_pt_br": row.get("thumbnail_title"),
        "english_narration_excerpt": english_text[:7000],
        "requirements": {
            "youtube_title_max_chars": 90,
            "thumbnail_title_max_chars": 45,
            "description": "2 to 4 short factual paragraphs in natural US English",
        },
    }
    message = client.messages.create(
        model=MODEL,
        max_tokens=1400,
        thinking={"type": "disabled"},
        system=system,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        output_config={"format": {"type": "json_schema", "schema": METADATA_SCHEMA}},
    )
    raw = extract_text(message)
    data = json.loads(raw)
    output = {
        "youtube_title": clean_text(str(data.get("youtube_title") or "")),
        "youtube_description": clean_text(str(data.get("youtube_description") or "")),
        "thumbnail_title": clean_text(str(data.get("thumbnail_title") or "")),
    }
    if not all(output.values()):
        raise RuntimeError("English metadata generation returned an empty required field")
    if len(output["youtube_title"]) > 90:
        raise RuntimeError("English YouTube title exceeds 90 characters")
    if len(output["thumbnail_title"]) > 45:
        raise RuntimeError("English thumbnail title exceeds 45 characters")
    return output


def main() -> None:
    require_env()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    row = fetch_queue()
    source_text = row["source_text"]
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    english_text = generate_english(client, source_text)
    metadata = generate_metadata(client, row, english_text)
    chunks = chunk_text(english_text)

    job = {
        "queue_id": row["id"],
        "blog_post_id": row.get("blog_post_id"),
        "source_language": "pt-BR",
        "target_language": "en-US",
        "channel_key": "leonidanos_en",
        "source_audio_uri": row.get("audio_url"),
        "source_word_count": count_words(source_text),
        "english_word_count": count_words(english_text),
        "text": english_text,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "shard_count": SHARD_COUNT,
        "voice": ENGLISH_VOICE,
        "metadata": metadata,
        "production_variant": True,
    }
    JOB_FILE.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT_DIR / "english-narration.txt").write_text(english_text + "\n", encoding="utf-8")
    (OUTPUT_DIR / "english-metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"queue_id={row['id']}\n")

    print(
        f"Prepared English production variant queue={row['id']} | "
        f"PT words={job['source_word_count']} | EN words={job['english_word_count']} | "
        f"chunks={job['chunk_count']} | title={metadata['youtube_title']}"
    )


if __name__ == "__main__":
    main()
