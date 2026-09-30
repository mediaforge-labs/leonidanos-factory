#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import requests

STOP = {
    'gta','vi','video','videos','game','jogo','rockstar','games','para','com','uma','que',
    'the','and','this','that','from','into','sobre','por','dos','das','nos','nas','seu','sua',
}

CONCEPTS = [
    ['sport','esporte','basquete','academia','musculacao','musculação','corrida','luta','bilhar','golfe','paraqued','caiaque','mergulho','jet','moto'],
    ['noite','boate','neon','night'],
    ['praia','pantano','pântano','natureza','beach'],
    ['lucia','lúcia'],
    ['jason'],
    ['carro','carros','veiculo','veículo','dirigir','corrida'],
    ['arma','armas','tiro','tiroteio','combate'],
    ['policia','polícia','perseguicao','perseguição'],
    ['vice city','cidade','rua','avenida'],
    ['barco','lancha','jetski','jet ski'],
]


def control(action: str, body: dict | None = None):
    base = os.environ['SUPABASE_URL'].rstrip('/')
    key = os.environ['SUPABASE_SECRET_KEY']
    url = f'{base}/functions/v1/mediaforge-dell-control'
    headers = {'apikey': key, 'Content-Type': 'application/json', 'User-Agent': 'MediaForge-GitHub/4.3'}
    if body is None:
        response = requests.get(url, headers=headers, params={'action': action}, timeout=60)
    else:
        response = requests.post(url, headers=headers, json={'action': action, **body}, timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(f'Dell control failed ({response.status_code}): {response.text[:500]}')
    return response.json()


def toks(value: str) -> set[str]:
    return {x for x in re.findall(r'[a-z0-9à-ÿ]{3,}', value.lower()) if x not in STOP}


def asset_text(asset: dict) -> str:
    return ' '.join(
        [
            str(asset.get('file_name') or ''),
            str(asset.get('relative_path') or ''),
            str(asset.get('description') or ''),
            str(asset.get('context') or ''),
            ' '.join(str(x) for x in (asset.get('tags') or [])),
        ]
    ).lower()


def asset_size(asset: dict) -> int:
    return max(0, int(asset.get('size_bytes') or 0))


def score(asset: dict, query: str) -> float:
    hay = asset_text(asset)
    q = toks(query)
    h = toks(hay)
    value = float(len(q & h) * 10)
    ql = query.lower()
    for terms in CONCEPTS:
        if any(term in ql for term in terms) and any(term in hay for term in terms):
            value += 9.0
    value += int(hashlib.sha256((query + str(asset['asset_id'])).encode()).hexdigest()[:4], 16) / 65535
    size_mb = asset_size(asset) / 1024 / 1024
    value -= min(size_mb, 250.0) * 0.004
    return value


def split_sentences(text: str) -> list[str]:
    text = re.sub(r'\s+', ' ', text or '').strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r'(?<=[.!?])\s+', text) if part.strip()]


def segment_script(text: str, target_words: int = 12, max_words: int = 18) -> list[str]:
    units: list[str] = []
    for sentence in split_sentences(text):
        words = sentence.split()
        if len(words) <= max_words:
            units.append(sentence)
            continue
        for start in range(0, len(words), target_words):
            chunk = ' '.join(words[start:start + target_words]).strip()
            if chunk:
                units.append(chunk)

    segments: list[str] = []
    current: list[str] = []
    count = 0
    for unit in units:
        n = len(unit.split())
        if current and count + n > max_words:
            segments.append(' '.join(current))
            current = []
            count = 0
        current.append(unit)
        count += n
        if count >= target_words:
            segments.append(' '.join(current))
            current = []
            count = 0
    if current:
        segments.append(' '.join(current))
    return segments


def choose_unique_pool(title: str, script: str, assets: list[dict], max_assets: int, byte_budget: int):
    if not assets:
        raise SystemExit('No approved Dell assets available')

    segments = segment_script(script)
    if not segments:
        raise SystemExit('Narration could not be segmented for semantic media selection')

    words = max(1, len(script.split()))
    estimated_scene_count = max(len(segments), math.ceil(words / 12))
    required_unique = min(len(assets), estimated_scene_count)
    if required_unique > max_assets:
        raise SystemExit(
            f'Media preflight requires at least {required_unique} unique clips for this narration, '
            f'but --max-assets={max_assets}. Increase the pool before TTS/render.'
        )

    smallest_required = sorted(asset_size(item) for item in assets)[:required_unique]
    minimum_possible_bytes = sum(smallest_required)
    if len(smallest_required) < required_unique or minimum_possible_bytes > byte_budget:
        raise SystemExit(
            f'Media preflight cannot fit {required_unique} unique clips inside the runner byte budget. '
            f'Minimum possible set needs {minimum_possible_bytes / 1024 / 1024 / 1024:.3f} GiB, '
            f'budget is {byte_budget / 1024 / 1024 / 1024:.3f} GiB.'
        )

    # A small alternatives buffer is enough. Keeping this bounded leaves disk for TTS,
    # the long-form render and Shorts instead of filling the runner with unused source files.
    target_pool = min(len(assets), max_assets, required_unique + 8)
    used: set[str] = set()
    chosen: list[dict] = []
    selection_rows: list[dict] = []
    total_bytes = 0

    def unused_assets() -> list[dict]:
        return [item for item in assets if str(item.get('asset_id')) not in used]

    def candidates_that_preserve_capacity(remaining_slots_after_pick: int) -> list[dict]:
        pool = unused_assets()
        if remaining_slots_after_pick <= 0:
            return [item for item in pool if total_bytes + asset_size(item) <= byte_budget]

        by_size = sorted(pool, key=asset_size)
        if len(by_size) <= remaining_slots_after_pick:
            return []

        reserve_base = by_size[:remaining_slots_after_pick]
        reserve_ids = {str(item['asset_id']) for item in reserve_base}
        reserve_sum = sum(asset_size(item) for item in reserve_base)
        replacement = asset_size(by_size[remaining_slots_after_pick])

        feasible: list[dict] = []
        for item in pool:
            item_id = str(item['asset_id'])
            item_bytes = asset_size(item)
            reserve = reserve_sum
            if item_id in reserve_ids:
                reserve = reserve_sum - item_bytes + replacement
            if total_bytes + item_bytes + reserve <= byte_budget:
                feasible.append(item)
        return feasible

    # One distinct source for every real narration segment.
    for segment_index, segment in enumerate(segments):
        remaining_required_after_pick = max(0, required_unique - len(chosen) - 1)
        candidates = candidates_that_preserve_capacity(remaining_required_after_pick)
        if not candidates:
            break
        query = title + '\n' + segment
        item = max(candidates, key=lambda candidate: score(candidate, query))
        aid = str(item['asset_id'])
        used.add(aid)
        total_bytes += asset_size(item)
        enriched = dict(item)
        original_context = str(enriched.get('context') or '').strip()
        enriched['context'] = (original_context + '\nNarration context: ' + segment).strip()
        enriched['mediaforge_segment_index'] = segment_index
        enriched['mediaforge_segment_text'] = segment
        enriched['mediaforge_selection_score'] = round(score(item, query), 4)
        enriched['mediaforge_max_uses'] = 1
        enriched['mediaforge_gta_vi_only'] = True
        chosen.append(enriched)
        selection_rows.append({'segment_index': segment_index, 'asset_id': aid, 'segment': segment, 'role': 'semantic-scene'})

    # Duration-based estimate can be a few clips larger than the semantic segmentation.
    whole_query = title + '\n' + script
    while len(chosen) < required_unique:
        remaining_required_after_pick = required_unique - len(chosen) - 1
        candidates = candidates_that_preserve_capacity(remaining_required_after_pick)
        if not candidates:
            break
        item = max(candidates, key=lambda candidate: (score(candidate, whole_query), -asset_size(candidate)))
        aid = str(item['asset_id'])
        used.add(aid)
        total_bytes += asset_size(item)
        enriched = dict(item)
        enriched['mediaforge_max_uses'] = 1
        enriched['mediaforge_gta_vi_only'] = True
        enriched['mediaforge_reserve_asset'] = True
        chosen.append(enriched)
        selection_rows.append({'segment_index': None, 'asset_id': aid, 'segment': None, 'role': 'unique-reserve'})

    if len(chosen) < required_unique:
        raise SystemExit(
            f'Media preflight selected only {len(chosen)} unique clips although {required_unique} are required. '
            f'No reuse was allowed and the remaining candidates could not fit the runner budget.'
        )

    remaining = unused_assets()
    remaining.sort(key=lambda item: (score(item, whole_query), -asset_size(item)), reverse=True)
    for item in remaining:
        if len(chosen) >= target_pool:
            break
        item_bytes = asset_size(item)
        if total_bytes + item_bytes > byte_budget:
            continue
        aid = str(item['asset_id'])
        used.add(aid)
        total_bytes += item_bytes
        enriched = dict(item)
        enriched['mediaforge_max_uses'] = 1
        enriched['mediaforge_gta_vi_only'] = True
        enriched['mediaforge_buffer_asset'] = True
        chosen.append(enriched)

    if len({str(item['asset_id']) for item in chosen}) != len(chosen):
        raise SystemExit('Internal media selector error: duplicate asset IDs in chosen pool')

    return chosen, selection_rows, segments, required_unique, total_bytes


def download_asset(status: dict, request_id: str, token: str, item: dict, out: pathlib.Path) -> tuple[bool, str, dict]:
    """Download one asset with transient retries and clean partial files.

    404/410 means the Dell catalog contains a stale entry. Those are returned to the
    caller so another unique GTA VI clip can replace it instead of killing the whole run.
    """
    current_status = status
    last_error = 'unknown'
    transient_codes = {408, 429, 500, 502, 503, 504}

    for attempt in range(1, 4):
        part = out.with_suffix(out.suffix + '.part')
        part.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        try:
            public_url = str(current_status.get('public_url') or '').rstrip('/')
            if not public_url:
                raise RuntimeError('Dell asset gateway public_url is empty')
            url = f"{public_url}/asset/{request_id}/{item['asset_id']}"
            request = urllib.request.Request(
                url,
                headers={'X-MediaForge-Request-Token': token, 'User-Agent': 'MediaForge-GitHub/4.3'},
            )
            with urllib.request.urlopen(request, timeout=1800) as source, part.open('wb') as target:
                while True:
                    chunk = source.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)

            expected = asset_size(item)
            actual = part.stat().st_size
            if expected > 0 and actual != expected:
                raise RuntimeError(f'size mismatch: expected={expected} actual={actual}')
            if actual <= 0:
                raise RuntimeError('downloaded file is empty')
            part.replace(out)
            return True, 'ok', current_status

        except urllib.error.HTTPError as exc:
            last_error = f'HTTP {exc.code}'
            part.unlink(missing_ok=True)
            if exc.code in {404, 410}:
                return False, last_error, current_status
            if exc.code not in transient_codes:
                return False, last_error, current_status
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, RuntimeError) as exc:
            last_error = f'{type(exc).__name__}: {exc}'
            part.unlink(missing_ok=True)

        if attempt < 3:
            # Refresh the tunnel URL/heartbeat before retrying in case the Dell gateway
            # rotated while this GitHub job was running.
            try:
                refreshed = control('status')
                if refreshed.get('status') == 'online' and refreshed.get('public_url'):
                    current_status = refreshed
            except Exception:
                pass
            time.sleep(2 * attempt)

    return False, last_error, current_status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--title', required=True)
    parser.add_argument('--script-file', required=True)
    parser.add_argument('--dest', required=True)
    parser.add_argument('--max-assets', type=int, default=140)
    parser.add_argument('--max-total-bytes', type=int, default=8 * 1024 * 1024 * 1024)
    parser.add_argument('--gateway-id', default='dell-main')
    parser.add_argument('--job-key', default='manual')
    args = parser.parse_args()

    script = pathlib.Path(args.script_file).read_text(encoding='utf-8')
    status = control('status')
    catalog = control('catalog')
    if status.get('status') != 'online' or not status.get('public_url'):
        raise SystemExit('Dell asset gateway is offline')
    beat = datetime.fromisoformat(str(status.get('heartbeat_at')).replace('Z', '+00:00'))
    if datetime.now(timezone.utc) - beat > timedelta(minutes=2):
        raise SystemExit('Dell asset gateway heartbeat is stale')

    # The Dell gateway is the owner-curated GTA VI library. External/random media is not
    # eligible. Every downloaded source is used at most once by the renderer.
    assets = [item for item in catalog.get('assets', []) if item.get('approved', True)]
    chosen, selection_rows, segments, required_unique, selected_bytes = choose_unique_pool(
        args.title, script, assets, args.max_assets, args.max_total_bytes
    )

    chosen_ids = {str(item['asset_id']) for item in chosen}
    fallback_pool = [item for item in assets if str(item.get('asset_id')) not in chosen_ids]

    request_id = uuid.uuid4().hex
    token = uuid.uuid4().hex + uuid.uuid4().hex
    # 4 hours avoids expiring a request during a slow transfer/render-preparation cycle.
    expires = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    # Authorize the curated catalog for this short-lived request so a stale/missing primary
    # can be replaced immediately without rebuilding the gateway request.
    control(
        'create_request',
        {
            'request_id': request_id,
            'request_token': token,
            'job_key': args.job_key,
            'asset_ids': [item['asset_id'] for item in assets],
            'expires_at': expires,
        },
    )

    dest = pathlib.Path(args.dest)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    local: list[dict] = []
    downloaded_ids: set[str] = set()
    unavailable_ids: set[str] = set()
    downloaded_bytes = 0
    current_status = status
    whole_query = args.title + '\n' + script

    def update_selection_for_replacement(original: dict, replacement: dict, reason: str) -> None:
        old_id = str(original['asset_id'])
        new_id = str(replacement['asset_id'])
        for row in selection_rows:
            if str(row.get('asset_id')) == old_id:
                row['asset_id'] = new_id
                row['replaced_from'] = old_id
                row['replacement_reason'] = reason

    def replacement_for(original: dict) -> dict | None:
        nonlocal downloaded_bytes
        query = args.title + '\n' + str(original.get('mediaforge_segment_text') or '')
        if not str(original.get('mediaforge_segment_text') or '').strip():
            query = whole_query
        candidates = [
            item for item in fallback_pool
            if str(item.get('asset_id')) not in downloaded_ids
            and str(item.get('asset_id')) not in unavailable_ids
            and downloaded_bytes + asset_size(item) <= args.max_total_bytes
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda candidate: (score(candidate, query), -asset_size(candidate)))

    try:
        planned = list(chosen)
        for index, original in enumerate(planned, 1):
            # If an asset was already consumed as a replacement, never download/reuse it again.
            if str(original['asset_id']) in downloaded_ids:
                continue

            item = original
            replacement_hops = 0
            while True:
                aid = str(item['asset_id'])
                suffix = pathlib.Path(str(item.get('file_name') or '')).suffix or '.mp4'
                out = dest / f'{len(local) + 1:03d}-{aid}{suffix}'
                ok, reason, current_status = download_asset(current_status, request_id, token, item, out)
                if ok:
                    actual = out.stat().st_size
                    if downloaded_bytes + actual > args.max_total_bytes:
                        out.unlink(missing_ok=True)
                        ok = False
                        reason = 'runner byte budget exceeded after download'
                    else:
                        downloaded_ids.add(aid)
                        downloaded_bytes += actual
                        enriched = dict(item)
                        enriched['local_name'] = out.name
                        enriched['mediaforge_max_uses'] = 1
                        enriched['mediaforge_gta_vi_only'] = True
                        local.append(enriched)
                        print(json.dumps({
                            'downloaded': len(local),
                            'planned': len(planned),
                            'asset_id': aid,
                            'bytes': actual,
                            'replacement': aid != str(original['asset_id']),
                        }))
                        break

                unavailable_ids.add(aid)
                print(json.dumps({
                    'asset_unavailable': aid,
                    'reason': reason,
                    'replacement_required': True,
                }))
                replacement = replacement_for(original)
                if replacement is None:
                    mandatory = bool(original.get('mediaforge_segment_index') is not None or original.get('mediaforge_reserve_asset'))
                    if mandatory or len(local) < required_unique:
                        raise SystemExit(
                            f'Unable to replace unavailable GTA VI asset {original["asset_id"]}; '
                            f'{len(local)}/{required_unique} required unique clips are locally verified.'
                        )
                    print(json.dumps({'optional_buffer_skipped': original['asset_id'], 'reason': reason}))
                    break

                replacement_hops += 1
                if replacement_hops > 12:
                    raise SystemExit(f'Too many unavailable replacement assets while replacing {original["asset_id"]}')

                replacement = dict(replacement)
                # Preserve the semantic assignment/role of the missing source.
                for key in (
                    'mediaforge_segment_index', 'mediaforge_segment_text', 'mediaforge_selection_score',
                    'mediaforge_reserve_asset', 'mediaforge_buffer_asset',
                ):
                    if key in original:
                        replacement[key] = original[key]
                replacement['mediaforge_max_uses'] = 1
                replacement['mediaforge_gta_vi_only'] = True
                replacement['mediaforge_replaced_asset_id'] = str(original['asset_id'])
                replacement['mediaforge_replacement_reason'] = reason
                update_selection_for_replacement(original, replacement, reason)
                item = replacement

        if len(local) < required_unique:
            raise SystemExit(
                f'Only {len(local)} locally verified unique GTA VI clips were downloaded; '
                f'{required_unique} are required. TTS/render will not start.'
            )
        if len(downloaded_ids) != len(local):
            raise SystemExit('Internal media download error: duplicate local asset IDs detected')
        if downloaded_bytes > args.max_total_bytes:
            raise SystemExit('Downloaded media exceeded the configured runner byte budget')

        # Every semantic segment must still point at a source that really exists locally,
        # including segments whose original catalog asset was replaced after a 404/410.
        local_ids = {str(item['asset_id']) for item in local}
        missing_segments = [
            row for row in selection_rows
            if row.get('segment_index') is not None and str(row.get('asset_id')) not in local_ids
        ]
        if missing_segments:
            raise SystemExit(
                f'{len(missing_segments)} narration segments have no locally verified GTA VI clip after fallback replacement.'
            )

        (dest / 'Catalogo geral.json').write_text(
            json.dumps(
                {
                    'scope': 'gta-vi-owner-curated-only',
                    'reuse_policy': 'never-reuse-source-file',
                    'assets': local,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding='utf-8',
        )
        (dest / 'mediaforge-selection.json').write_text(
            json.dumps(
                {
                    'scope': 'gta-vi-owner-curated-only',
                    'segments': segments,
                    'segment_asset_map': selection_rows,
                    'required_unique_assets': required_unique,
                    'planned_unique_assets': len(chosen),
                    'downloaded_unique_assets': len(local),
                    'downloaded_bytes': downloaded_bytes,
                    'unavailable_catalog_assets': sorted(unavailable_ids),
                    'reuse_allowed': False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding='utf-8',
        )
    finally:
        try:
            control('delete_request', {'request_id': request_id})
        except Exception:
            pass

    print(json.dumps({
        'status': 'ready',
        'root': str(dest.resolve()),
        'planned': len(chosen),
        'downloaded_unique': len(local),
        'required_unique': required_unique,
        'segments': len(segments),
        'downloaded_gb': round(downloaded_bytes / 1024 / 1024 / 1024, 3),
        'catalog_assets_skipped': len(unavailable_ids),
        'reuse_allowed': False,
        'scope': 'gta-vi-owner-curated-only',
        'request_id': request_id,
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
