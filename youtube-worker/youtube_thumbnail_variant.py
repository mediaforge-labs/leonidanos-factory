from __future__ import annotations

import base64
import html
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import requests

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SECRET_KEY") or "").strip()
QUEUE_ID = os.getenv("LEONIDANOS_QUEUE_ID", "").strip()
LOCALE = os.getenv("LEONIDANOS_VARIANT_LOCALE", "en-US").strip()
BUCKET = "youtube-assets"
OUTPUT_DIR = Path("youtube-worker/output/thumbnail-variant")
WIDTH = 1920
HEIGHT = 1080
OFFICIAL_BG = "https://www.rockstargames.com/VI/_next/static/media/Official_Cover_Art_landscape.12.uu2irr.2_a.jpg?akim=1&imdensity=1&imwidth=2560"
OFFICIAL_LOGO = "https://www.rockstargames.com/VI/_next/static/media/vi.0hxa9~pf214xe.png?akim=1&imdensity=1&imwidth=3840"


def require_env() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL and Supabase service key are required")
    if LOCALE not in {"pt-BR", "en-US"}:
        raise RuntimeError(f"Unsupported locale: {LOCALE}")


def headers(prefer: str | None = None) -> dict[str, str]:
    out = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
    if not SUPABASE_KEY.startswith("sb_secret_"):
        out["Authorization"] = f"Bearer {SUPABASE_KEY}"
    if prefer:
        out["Prefer"] = prefer
    return out


def sb_get(table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    response = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=headers(), params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def sb_patch(table: str, params: dict[str, str], body: dict[str, Any]) -> list[dict[str, Any]]:
    response = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}", headers=headers("return=representation"), params=params, json=body, timeout=30
    )
    response.raise_for_status()
    return response.json() if response.content else []


def fetch_variant() -> dict[str, Any]:
    params = {
        "select": "id,queue_id,blog_post_id,locale,status,thumbnail_title,thumbnail_url,youtube_privacy_status,updated_at",
        "locale": f"eq.{LOCALE}",
        "thumbnail_title": "not.is.null",
        "order": "updated_at.desc",
        "limit": "1",
    }
    if QUEUE_ID:
        params["queue_id"] = f"eq.{QUEUE_ID}"
    rows = sb_get("youtube_video_variants", params)
    if not rows:
        raise RuntimeError(f"No {LOCALE} variant is ready for thumbnail")
    row = rows[0]
    if row.get("youtube_privacy_status") != "private":
        raise RuntimeError("Safety block: thumbnail variant is not PRIVATE")
    if row.get("status") not in {"script_ready", "video_ready", "thumbnail_ready", "upload_ready"}:
        raise RuntimeError(f"Unsupported variant status: {row.get('status')}")
    return row


def fetch_queue(queue_id: str) -> dict[str, Any]:
    rows = sb_get("youtube_queue", {
        "select": "id,thumbnail_image_url,thumbnail_arrow_enabled,thumbnail_arrow_preset",
        "id": f"eq.{queue_id}", "limit": "1",
    })
    return rows[0] if rows else {}


def fetch_post(blog_post_id: str) -> dict[str, Any]:
    rows = sb_get("blog_posts", {
        "select": "id,title,content,featured_image_url", "id": f"eq.{blog_post_id}", "limit": "1",
    })
    return rows[0] if rows else {}


def extract_image_urls(content: str) -> list[str]:
    pattern = re.compile(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\']', re.I)
    output: list[str] = []
    for match in pattern.finditer(content or ""):
        url = html.unescape(match.group(1)).strip()
        if url.startswith(("http://", "https://")) and url not in output:
            output.append(url)
    return output


def leak_topic(post: dict[str, Any]) -> bool:
    title = str(post.get("title") or "").lower()
    return any(term in title for term in ("vazamento", "vazado", "vazada", "leak", "rumor"))


def official_url(url: str) -> bool:
    low = url.lower()
    return "rockstargames.com" in low or "gtavi-thealbum.com" in low


def background_candidates(queue: dict[str, Any], post: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    override = str(queue.get("thumbnail_image_url") or "").strip()
    featured = str(post.get("featured_image_url") or "").strip()
    sensitive = leak_topic(post)
    if override:
        candidates.append(override)
    if featured and (not sensitive or official_url(featured)):
        candidates.append(featured)
    for url in extract_image_urls(str(post.get("content") or "")):
        if not sensitive or official_url(url):
            candidates.append(url)
    candidates.append(OFFICIAL_BG)
    unique: list[str] = []
    for url in candidates:
        if url.startswith(("http://", "https://")) and url not in unique:
            unique.append(url)
    return unique


def download(url: str) -> bytes:
    response = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0 LeonidanosFactory"})
    response.raise_for_status()
    if len(response.content) < 1024:
        raise RuntimeError(f"Image too small: {url}")
    return response.content


def convert_image(data: bytes, stem: str, *, width: int | None = None) -> bytes:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    src = OUTPUT_DIR / f"{stem}-src.img"
    dst = OUTPUT_DIR / f"{stem}.jpg"
    src.write_bytes(data)
    scale = f"scale={width}:-2" if width else "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080"
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", scale, "-frames:v", "1", "-q:v", "3", str(dst)],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0 or not dst.exists():
        raise RuntimeError("Image conversion failed: " + (result.stderr or result.stdout))
    return dst.read_bytes()


def data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def normalize_title(value: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(value or "").replace("\n", " ").strip().upper())
    if not text:
        return ["GTA 6", "NEWS"] if LOCALE == "en-US" else ["NOVIDADES", "GTA 6"]
    words = text.split()
    if len(text) <= 15:
        return [text]
    best: tuple[int, list[str]] | None = None
    for idx in range(1, len(words)):
        left, right = " ".join(words[:idx]), " ".join(words[idx:])
        score = max(len(left), len(right)) * 10 + abs(len(left) - len(right))
        if best is None or score < best[0]:
            best = (score, [left, right])
    return (best[1] if best else [text])[:2]


def build_svg(title: str, bg_uri: str, logo_uri: str | None, arrow_enabled: bool, arrow_preset: str) -> str:
    lines = normalize_title(title)
    longest = max(len(x) for x in lines)
    size = 194 if longest <= 15 else 172 if longest <= 18 else 150 if longest <= 22 else 132
    line_gap = round(size * 0.9, 2)
    baseline = 920 if len(lines) == 1 else 818
    tspans = "".join(
        f'<tspan x="81" dy="{0 if i == 0 else line_gap}">{html.escape(line)}</tspan>' for i, line in enumerate(lines)
    )
    logo = f'<image href="{logo_uri}" x="81" y="90" width="314" height="238" preserveAspectRatio="xMidYMid meet"/>' if logo_uri else ""
    arrow_presets = {
        "left": (190, 425, -18), "right": (1370, 410, 162), "top_left": (420, 155, 18),
        "top_right": (1270, 155, 162), "bottom_left": (210, 635, -35), "bottom_right": (1390, 635, 215),
    }
    arrow = ""
    if arrow_enabled and arrow_preset in arrow_presets:
        x, y, rotation = arrow_presets[arrow_preset]
        arrow = f'''<g transform="translate({x} {y}) rotate({rotation} 175 175)"><path d="M32 188 C88 94 164 62 252 118" fill="none" stroke="#080808" stroke-width="42" stroke-linecap="round"/><path d="M32 188 C88 94 164 62 252 118" fill="none" stroke="#fff" stroke-width="25" stroke-linecap="round"/><path d="M32 188 C88 94 164 62 252 118" fill="none" stroke="#f21f83" stroke-width="8" stroke-linecap="round"/><path d="M225 72 L319 129 L221 169 Z" fill="#fff" stroke="#080808" stroke-width="26" stroke-linejoin="round"/><path d="M225 72 L319 129 L221 169 Z" fill="#fff" stroke="#f21f83" stroke-width="7" stroke-linejoin="round"/></g>'''
    black_stroke = round(size * 0.1783 * 2, 2)
    pink_stroke = round(size * 0.0669 * 2, 2)
    letter_spacing = round(size * -0.04, 2)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080"><defs><linearGradient id="whiteFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#fff"/><stop offset="74%" stop-color="#fff"/><stop offset="100%" stop-color="#b9b9b9"/></linearGradient><linearGradient id="pinkStroke" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#f71c89"/><stop offset="100%" stop-color="#d7025e"/></linearGradient><linearGradient id="shade" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#000" stop-opacity="0"/><stop offset="100%" stop-color="#000" stop-opacity="0.22"/></linearGradient></defs><image href="{bg_uri}" x="0" y="0" width="1920" height="1080" preserveAspectRatio="xMidYMid slice"/><rect x="0" y="480" width="1920" height="600" fill="url(#shade)"/>{logo}{arrow}<g font-family="Montserrat" font-weight="900" font-size="{size}px" letter-spacing="{letter_spacing}px" xml:space="preserve"><text x="81" y="{baseline}" fill="none" stroke="#050505" stroke-width="{black_stroke}" stroke-linejoin="round">{tspans}</text><text x="81" y="{baseline}" fill="none" stroke="url(#pinkStroke)" stroke-width="{pink_stroke}" stroke-linejoin="round">{tspans}</text><text x="81" y="{baseline}" fill="url(#whiteFill)">{tspans}</text></g></svg>'''


def render_svg(svg: str, output: Path) -> None:
    svg_path = OUTPUT_DIR / "thumbnail.svg"
    svg_path.write_text(svg, encoding="utf-8")
    result = subprocess.run(["rsvg-convert", "-w", "1920", "-h", "1080", "-o", str(output), str(svg_path)], capture_output=True, text=True, timeout=180)
    if result.returncode != 0 or not output.exists():
        raise RuntimeError("SVG render failed: " + (result.stderr or result.stdout))


def upload_png(queue_id: str, png_path: Path) -> str:
    object_path = f"thumbnails/{queue_id}/{LOCALE}/thumbnail.png"
    storage_headers = {"apikey": SUPABASE_KEY, "Content-Type": "image/png", "x-upsert": "true"}
    if not SUPABASE_KEY.startswith("sb_secret_"):
        storage_headers["Authorization"] = f"Bearer {SUPABASE_KEY}"
    with png_path.open("rb") as fh:
        response = requests.post(f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{object_path}", headers=storage_headers, data=fh, timeout=180)
    response.raise_for_status()
    return f"supabase://{BUCKET}/{object_path}"


def main() -> None:
    require_env()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    variant = fetch_variant()
    queue = fetch_queue(str(variant["queue_id"]))
    post = fetch_post(str(variant.get("blog_post_id") or ""))
    background: bytes | None = None
    for candidate in background_candidates(queue, post):
        try:
            background = convert_image(download(candidate), "background")
            break
        except Exception as exc:
            print(f"THUMBNAIL_BACKGROUND_SKIP {candidate}: {exc}")
    if background is None:
        raise RuntimeError("No thumbnail background could be rendered")
    logo_uri = None
    try:
        logo_uri = data_uri(convert_image(download(OFFICIAL_LOGO), "logo", width=600), "image/jpeg")
    except Exception as exc:
        print(f"THUMBNAIL_LOGO_SKIP {exc}")
    svg = build_svg(
        str(variant.get("thumbnail_title") or ""), data_uri(background, "image/jpeg"), logo_uri,
        bool(queue.get("thumbnail_arrow_enabled")), str(queue.get("thumbnail_arrow_preset") or "none"),
    )
    output = OUTPUT_DIR / "thumbnail.png"
    render_svg(svg, output)
    uri = upload_png(str(variant["queue_id"]), output)
    current = str(variant.get("status") or "")
    next_status = "upload_ready" if current in {"video_ready", "upload_ready"} else "thumbnail_ready"
    updated = sb_patch("youtube_video_variants", {"id": f"eq.{variant['id']}"}, {
        "thumbnail_url": uri, "status": next_status, "last_error": None,
    })
    if not updated:
        raise RuntimeError("Thumbnail uploaded but variant checkpoint was not updated")
    print(f"THUMBNAIL_OK locale={LOCALE} queue={variant['queue_id']} status={next_status} uri={uri}")


if __name__ == "__main__":
    main()
