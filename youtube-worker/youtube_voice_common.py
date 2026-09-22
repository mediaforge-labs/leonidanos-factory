import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VOICE = {
    "label": "leonidanos-oficial-gamer-maxima-energia",
    "engine": "chatterbox-multilingual-v3",
    "language": "pt",
    "seed": 260918,
    "temperature": 0.55,
    "exaggeration": 1.05,
    "cfg_weight": 0.25,
    "repetition_penalty": 1.2,
    "min_p": 0.05,
    "top_p": 1.0,
    "tempo": 1.06,
    "loudnorm": "I=-16:TP=-1.5:LRA=9",
}

MIN_SOURCE_WORDS = 900
MIN_FINAL_SECONDS = 300.0
TARGET_CHUNK_CHARS = 480
MAX_CHUNK_CHARS = 620
SHARD_COUNT = 4

MONTH_NAMES = (
    "janeiro|fevereiro|março|marco|abril|maio|junho|julho|agosto|"
    "setembro|outubro|novembro|dezembro"
)

PT_TTS_PRONUNCIATIONS: tuple[tuple[str, str], ...] = (
    (r"\bRockstar\s+Games\b", "Rókstar Gueimes"),
    (r"\bRockstar\b", "Rókstar"),
    (r"\bTake[-\s]?Two\b", "Teique Tú"),
    (r"\bJason\b", "Jeison"),
    (r"\bgamer\b", "gueimer"),
    (r"\bGamers\b", "Gueimers"),
    (r"\bestreia\b", "estréia"),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def count_words(text: str) -> int:
    return len(re.findall(r"\b[\wÀ-ÿ'-]+\b", text or "", flags=re.UNICODE))


def apply_pt_pronunciations(text: str) -> str:
    text = re.sub(r"\bG\s*T\s*A\s*(?:VI|6)\b", "Gê Tê A seis", text, flags=re.IGNORECASE)
    text = re.sub(r"\bG\s*T\s*A\s*(?:V|5)\b", "Gê Tê A cinco", text, flags=re.IGNORECASE)
    text = re.sub(r"\bG\s*T\s*A\b", "Gê Tê A", text, flags=re.IGNORECASE)
    for pattern, spoken in PT_TTS_PRONUNCIATIONS:
        text = re.sub(pattern, spoken, text, flags=re.IGNORECASE)
    text = re.sub(r"\bVI\b", "seis", text)
    return text


def normalize_text(text: str) -> str:
    text = (text or "").replace("```", "").replace("`", "")
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = apply_pt_pronunciations(text)
    text = re.sub(
        rf"\b(\d{{1,2}})\s+de\s+({MONTH_NAMES})\b",
        r"\1, de \2",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*([,;:!?])\s*", r"\1 ", text)
    text = re.sub(r"\s*\.\s*", ". ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_long_unit(unit: str) -> list[str]:
    unit = unit.strip()
    if len(unit) <= MAX_CHUNK_CHARS:
        return [unit]
    pieces = re.split(r"(?<=[,;:])\s+", unit)
    if len(pieces) == 1:
        words = unit.split()
        pieces = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and len(candidate) > MAX_CHUNK_CHARS:
                pieces.append(current)
                current = word
            else:
                current = candidate
        if current:
            pieces.append(current)
        return pieces
    output: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current} {piece}".strip()
        if current and len(candidate) > MAX_CHUNK_CHARS:
            output.append(current)
            current = piece
        else:
            current = candidate
    if current:
        output.append(current)
    return output


def chunk_text(text: str) -> list[dict[str, Any]]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    raw_chunks: list[dict[str, Any]] = []
    for paragraph in paragraphs:
        sentence_units: list[str] = []
        for sentence in re.split(r"(?<=[.!?…])\s+", paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            sentence_units.extend(split_long_unit(sentence))
        paragraph_chunks: list[str] = []
        current = ""
        for unit in sentence_units:
            candidate = f"{current} {unit}".strip()
            if current and len(candidate) > TARGET_CHUNK_CHARS:
                paragraph_chunks.append(current)
                current = unit
            else:
                current = candidate
        if current:
            paragraph_chunks.append(current)
        for index, chunk in enumerate(paragraph_chunks):
            raw_chunks.append({"text": chunk, "paragraph_end": index == len(paragraph_chunks) - 1})
    if not raw_chunks:
        raise RuntimeError("TTS chunking produced zero chunks")
    chunks: list[dict[str, Any]] = []
    for index, item in enumerate(raw_chunks, start=1):
        pause = 0.0
        if index < len(raw_chunks):
            pause = 0.42 if item["paragraph_end"] else 0.20
        chunks.append(
            {
                "index": index,
                "text": item["text"],
                "chars": len(item["text"]),
                "words": count_words(item["text"]),
                "paragraph_end": bool(item["paragraph_end"]),
                "pause_after_seconds": pause,
            }
        )
    return chunks


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
