#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import subprocess
from typing import Any

VIDEO_EXTENSIONS = ('.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi')
IDENTITY_KEYS = (
    'media_relative_path', 'asset_id', 'source_asset_id', 'video_asset_id',
    'relative_path', 'media_path', 'video_path', 'source_path', 'local_name',
)
SCENE_MARKERS = {
    'scene_index', 'start_seconds', 'end_seconds', 'duration_seconds',
    'narration_text', 'semantic_query', 'source_in_seconds',
}


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def identity_from_dict(node: dict[str, Any]) -> str | None:
    for key in IDENTITY_KEYS:
        value = node.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            text = str(value).strip()
            if key.endswith('path') or key == 'local_name':
                if text.lower().endswith(VIDEO_EXTENSIONS):
                    return text.replace('\\', '/').lower()
            else:
                return text.lower()
    for key, value in node.items():
        if not isinstance(value, str):
            continue
        low_key = key.lower()
        if any(term in low_key for term in ('video', 'media', 'asset', 'source')) and value.lower().endswith(VIDEO_EXTENSIONS):
            return value.replace('\\', '/').lower()
    return None


def collect_scene_usage(node: Any, output: list[str]) -> None:
    if isinstance(node, dict):
        keys = set(node)
        identity = identity_from_dict(node)
        if identity and len(keys & SCENE_MARKERS) >= 1:
            output.append(identity)
            return
        for value in node.values():
            collect_scene_usage(value, output)
    elif isinstance(node, list):
        for value in node:
            collect_scene_usage(value, output)


def validate_selection(selection_path: pathlib.Path) -> tuple[dict[str, Any], list[str]]:
    selection = load(selection_path)
    selected = int(
        selection.get('downloaded_unique_assets')
        or selection.get('selected_unique_assets')
        or 0
    )
    required = int(selection.get('required_unique_assets') or 0)
    all_rows = selection.get('segment_asset_map') or []
    semantic_rows = [row for row in all_rows if row.get('segment_index') is not None]
    ids = [str(row.get('asset_id')).strip().lower() for row in semantic_rows if row.get('asset_id')]
    segments = selection.get('segments') or []

    if selection.get('reuse_allowed') is not False:
        raise SystemExit('selection manifest does not enforce reuse_allowed=false')
    if selection.get('scope') != 'gta-vi-owner-curated-only':
        raise SystemExit(f"selection scope is not owner-curated GTA VI: {selection.get('scope')}")
    if selected < required or required < 1:
        raise SystemExit(f'unique media pool is insufficient: selected={selected}, required={required}')
    if not ids:
        raise SystemExit('selection manifest has no per-segment asset IDs')
    if len(ids) != len(set(ids)):
        duplicates = [item for item, count in collections.Counter(ids).items() if count > 1]
        raise SystemExit(f'semantic selector reused asset IDs before render: {duplicates[:8]}')
    if segments and len(ids) != len(segments):
        raise SystemExit(
            f'semantic media map is incomplete: mapped={len(ids)}, segments={len(segments)}'
        )

    return ({
        'selected_unique_assets': selected,
        'required_unique_assets': required,
        'mapped_segments': len(ids),
        'scope': selection.get('scope'),
    }, ids)


def ffprobe(path: pathlib.Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            'ffprobe', '-v', 'error',
            '-show_entries', 'stream=index,codec_type,codec_name,width,height:format=duration',
            '-of', 'json', str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=90,
    )
    payload = json.loads(result.stdout or '{}')
    duration = float((payload.get('format') or {}).get('duration') or 0.0)
    streams = payload.get('streams') or []
    return {'duration': duration, 'streams': streams}


def validate_long_form(path: pathlib.Path, narration_seconds: float) -> dict[str, Any]:
    probe = ffprobe(path)
    video_streams = [s for s in probe['streams'] if s.get('codec_type') == 'video']
    audio_streams = [s for s in probe['streams'] if s.get('codec_type') == 'audio']
    if not video_streams:
        raise SystemExit('long-form output has no decodable video stream')
    if not audio_streams:
        raise SystemExit('long-form output has no audio stream')
    width = int(video_streams[0].get('width') or 0)
    height = int(video_streams[0].get('height') or 0)
    duration = float(probe['duration'])
    if width < 1280 or height < 720 or width <= height:
        raise SystemExit(f'long-form geometry is invalid: {width}x{height}')
    if duration < 60:
        raise SystemExit(f'long-form duration is unexpectedly short: {duration:.2f}s')
    if narration_seconds > 0:
        ratio = duration / narration_seconds
        if ratio < 0.90 or ratio > 1.15:
            raise SystemExit(
                f'long-form duration diverges from narration: video={duration:.2f}s narration={narration_seconds:.2f}s ratio={ratio:.3f}'
            )
    return {
        'width': width,
        'height': height,
        'duration_seconds': duration,
        'video_codec': video_streams[0].get('codec_name'),
        'audio_codec': audio_streams[0].get('codec_name'),
    }


def validate_short(path: pathlib.Path) -> dict[str, Any]:
    if path.stat().st_size < 50_000:
        raise SystemExit(f'Short output is unexpectedly small: {path}')
    probe = ffprobe(path)
    video_streams = [s for s in probe['streams'] if s.get('codec_type') == 'video']
    audio_streams = [s for s in probe['streams'] if s.get('codec_type') == 'audio']
    if not video_streams:
        raise SystemExit(f'Short has no video stream: {path}')
    if not audio_streams:
        raise SystemExit(f'Short has no audio stream: {path}')
    width = int(video_streams[0].get('width') or 0)
    height = int(video_streams[0].get('height') or 0)
    duration = float(probe['duration'])
    if width < 1 or height < 1 or height <= width:
        raise SystemExit(f'Short is not vertical: {path} {width}x{height}')
    if duration <= 0 or duration > 180:
        raise SystemExit(f'Short duration is invalid: {path} {duration:.2f}s')
    return {'file': path.name, 'width': width, 'height': height, 'duration_seconds': duration}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=pathlib.Path)
    parser.add_argument('--selection', type=pathlib.Path)
    parser.add_argument('--require-five-shorts', action='store_true')
    args = parser.parse_args()

    root = args.root
    manifest_path = root / 'manifest.json'
    media_manifest_path = root / 'media' / 'manifest.json'
    longform = root / 'video' / 'long-form.mp4'
    narration = root / 'audio' / 'narration.wav'
    captions = root / 'captions' / 'long-form.srt'

    for path in (manifest_path, media_manifest_path, longform, narration, captions):
        if not path.is_file() or path.stat().st_size <= 0:
            raise SystemExit(f'required render output missing: {path}')
    if longform.stat().st_size < 100000:
        raise SystemExit('long-form output is unexpectedly small')

    manifest = load(manifest_path)
    metrics = manifest.get('metrics') or {}
    if int(metrics.get('video_assets') or 0) < 1:
        raise SystemExit('render manifest reports no video assets')
    music_tracks = int(metrics.get('music_tracks') or 0)
    if music_tracks != 0:
        raise SystemExit(f'production contract requires music disabled, got music_tracks={music_tracks}')
    narration_seconds = float(metrics.get('narration_seconds') or 0.0)
    if narration_seconds <= 0:
        raise SystemExit('render manifest reports invalid narration duration')

    shorts = sorted((root / 'shorts').glob('short-*.mp4'))
    if args.require_five_shorts and len(shorts) != 5:
        raise SystemExit(f'expected exactly 5 Shorts, got {len(shorts)}')

    selection_result = None
    selection_usage: list[str] = []
    if args.selection:
        selection_result, selection_usage = validate_selection(args.selection)

    media_manifest = load(media_manifest_path)
    usage: list[str] = []
    collect_scene_usage(media_manifest, usage)
    verification_source = 'media_manifest'

    if not usage:
        if selection_usage:
            usage = selection_usage
            verification_source = 'selection_manifest'
        else:
            raise SystemExit('could not verify per-scene media usage from media/manifest.json or selection manifest')
    elif selection_result and len(usage) != selection_result['mapped_segments']:
        raise SystemExit(
            'media manifest scene coverage disagrees with semantic selection: '
            f"media_manifest={len(usage)}, selection={selection_result['mapped_segments']}"
        )

    counts = collections.Counter(usage)
    repeated = {item: count for item, count in counts.items() if count > 1}
    if repeated:
        sample = list(repeated.items())[:10]
        raise SystemExit(f'RENDER INVALID: source video reused across scenes: {sample}')

    long_form_probe = validate_long_form(longform, narration_seconds)
    short_probes = [validate_short(path) for path in shorts]

    print(json.dumps({
        'status': 'validated',
        'long_form_bytes': longform.stat().st_size,
        'long_form': long_form_probe,
        'shorts': len(shorts),
        'short_probes': short_probes,
        'scene_media_uses': len(usage),
        'unique_scene_media': len(counts),
        'duplicates': 0,
        'media_verification_source': verification_source,
        'selection': selection_result,
        'music_tracks': music_tracks,
        'narration_seconds': narration_seconds,
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
