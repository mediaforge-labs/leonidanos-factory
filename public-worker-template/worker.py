#!/usr/bin/env python3
"""Public MediaForge worker bootstrap.

Decrypts the encrypted proprietary core into a temporary directory and invokes
its entrypoint without persisting plaintext core files in the repository.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import pathlib
import re
import subprocess
import tarfile
import tempfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"MFP1"
NONCE_SIZE = 12
KEY_ENV = "MEDIAFORGE_CORE_KEY_B64"
TRUE_VALUES = {"1", "true", "yes", "on"}


def enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


def load_key() -> bytes:
    raw = os.environ.get(KEY_ENV, "")
    if not raw:
        raise SystemExit(f"Missing required environment variable: {KEY_ENV}")
    key = base64.b64decode(raw, validate=True)
    if len(key) != 32:
        raise SystemExit(f"{KEY_ENV} must decode to exactly 32 bytes")
    return key


def decrypt_bundle(path: pathlib.Path, key: bytes) -> bytes:
    blob = path.read_bytes()
    if len(blob) <= len(MAGIC) + NONCE_SIZE or not blob.startswith(MAGIC):
        raise SystemExit("Invalid MediaForge encrypted bundle")
    nonce = blob[len(MAGIC):len(MAGIC) + NONCE_SIZE]
    ciphertext = blob[len(MAGIC) + NONCE_SIZE:]
    return AESGCM(key).decrypt(nonce, ciphertext, MAGIC)


def safe_extract_tar_gz(payload: bytes, destination: pathlib.Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        root = destination.resolve()
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if root != target and root not in target.parents:
                raise SystemExit(f"Unsafe archive member: {member.name}")
        archive.extractall(destination)


def apply_runtime_source_hardening(core_dir: pathlib.Path) -> dict[str, int]:
    """Harden only the ephemeral decrypted copy; plaintext is never committed."""
    stats = {
        "python_sources": 0,
        "unique_media_sources": 0,
        "unique_media_patches": 0,
        "tts_sources_seen": 0,
        "tts_parallel_sources": 0,
        "syntax_verified_sources": 0,
    }

    for path in core_dir.rglob("*.py"):
        stats["python_sources"] += 1
        text = path.read_text(encoding="utf-8", errors="ignore")
        original = text
        lower = text.lower()

        if "chunk" in lower and ("wav" in lower or "tts" in lower):
            stats["tts_sources_seen"] += 1
            if "threadpoolexecutor" in lower or "processpoolexecutor" in lower or "as_completed" in lower:
                stats["tts_parallel_sources"] += 1

        # Older private cores used a soft reuse penalty. If that implementation is
        # present, harden it in-place. Newer cores may consume an already-unique
        # external selection manifest instead; that path is verified separately below.
        if "use_counts" in text and "relative_path" in text:
            stats["unique_media_sources"] += 1
            pattern = re.compile(
                r'(?P<indent>\s*)path\s*=\s*candidate\[(["\'])relative_path\2\]\s*\n(?!\s*if\s+use_counts)',
                re.MULTILINE,
            )

            def add_guard(match: re.Match[str]) -> str:
                indent = match.group("indent")
                quote = match.group(2)
                return (
                    f"{indent}path = candidate[{quote}relative_path{quote}]\n"
                    f"{indent}if use_counts.get(path, 0) > 0:\n"
                    f"{indent}    continue\n"
                )

            text, count = pattern.subn(add_guard, text)
            stats["unique_media_patches"] += count
            text = re.sub(
                r'^\s*score\s*-=?\s*use_counts\[path\]\s*\*\s*[0-9.]+\s*$',
                '',
                text,
                flags=re.MULTILINE,
            )

        if text != original:
            path.write_text(text, encoding="utf-8")

    # Compile every private Python source after hotfixing. This catches a bad runtime
    # patch now, before Chatterbox synthesis or FFmpeg rendering consumes an hour.
    for path in core_dir.rglob("*.py"):
        source = path.read_text(encoding="utf-8", errors="ignore")
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            raise SystemExit(
                f"Private core hardening produced invalid Python before TTS/render: "
                f"{path.relative_to(core_dir)}:{exc.lineno}: {exc.msg}"
            ) from exc
        stats["syntax_verified_sources"] += 1

    return stats


def verify_external_unique_media_guard() -> dict[str, object]:
    """Verify the Dell selector proof used by the current V7 production core.

    The public Dell selector creates an immutable local pool plus a semantic segment map
    before the private core starts. This is a valid strict no-reuse boundary even when
    the private core no longer contains the legacy ``use_counts`` selector that older
    bundles used. The final render validator still checks actual per-scene usage again.
    """
    root_raw = os.environ.get("MEDIAFORGE_VIDEO_LIBRARY_ROOT", "").strip()
    result: dict[str, object] = {
        "verified": False,
        "root_present": False,
        "selection_present": False,
        "catalog_present": False,
        "selected_unique_assets": 0,
        "required_unique_assets": 0,
        "mapped_segments": 0,
        "catalog_assets": 0,
        "scope": None,
    }
    if not root_raw:
        return result

    root = pathlib.Path(root_raw)
    result["root_present"] = root.is_dir()
    if not root.is_dir():
        return result

    selection_path = root / "mediaforge-selection.json"
    catalog_path = root / "Catalogo geral.json"
    result["selection_present"] = selection_path.is_file()
    result["catalog_present"] = catalog_path.is_file()
    if not selection_path.is_file() or not catalog_path.is_file():
        return result

    try:
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result

    selected = int(selection.get("downloaded_unique_assets") or selection.get("selected_unique_assets") or 0)
    required = int(selection.get("required_unique_assets") or 0)
    segments = selection.get("segments") or []
    semantic_rows = [
        row for row in (selection.get("segment_asset_map") or [])
        if isinstance(row, dict) and row.get("segment_index") is not None
    ]
    mapped_ids = [str(row.get("asset_id") or "").strip() for row in semantic_rows]
    mapped_ids = [value for value in mapped_ids if value]
    assets = [row for row in (catalog.get("assets") or []) if isinstance(row, dict)]
    catalog_ids = [str(row.get("asset_id") or "").strip() for row in assets]
    local_names = [str(row.get("local_name") or "").strip() for row in assets]

    result.update({
        "selected_unique_assets": selected,
        "required_unique_assets": required,
        "mapped_segments": len(mapped_ids),
        "catalog_assets": len(assets),
        "scope": selection.get("scope"),
    })

    if selection.get("reuse_allowed") is not False:
        return result
    if selection.get("scope") != "gta-vi-owner-curated-only":
        return result
    if selected < required or required < 1:
        return result
    if len(mapped_ids) != len(set(mapped_ids)):
        return result
    if segments and len(mapped_ids) != len(segments):
        return result
    if len(assets) != selected:
        return result
    if not catalog_ids or len(catalog_ids) != len(set(catalog_ids)):
        return result
    if not local_names or len(local_names) != len(set(local_names)):
        return result

    for name in local_names:
        path = root / name
        try:
            resolved = path.resolve()
            resolved.relative_to(root.resolve())
        except Exception:
            return result
        if not resolved.is_file() or resolved.stat().st_size <= 0:
            return result

    result["verified"] = True
    return result


def verify_runtime_hardening(stats: dict[str, int]) -> None:
    """Fail before TTS/render unless strict guarantees are actually present."""
    strict_unique = enabled("MEDIAFORGE_STRICT_UNIQUE_MEDIA", True)
    strict_tts = enabled("MEDIAFORGE_TTS_STRICT_CHUNK_ORDER", True)
    errors: list[str] = []

    if stats["python_sources"] < 1 or stats["syntax_verified_sources"] != stats["python_sources"]:
        errors.append("not every private Python source passed post-hardening syntax validation")

    private_unique_guard = stats["unique_media_sources"] >= 1 and stats["unique_media_patches"] >= 1
    external_unique_guard = verify_external_unique_media_guard() if strict_unique else {"verified": False}

    if strict_unique and not private_unique_guard and not bool(external_unique_guard.get("verified")):
        errors.append(
            "strict no-reuse proof missing: neither a hardened private selector nor a verified external unique-media manifest was found"
        )

    if strict_tts and stats["tts_sources_seen"] < 1:
        errors.append("no private TTS/chunk source was found for ordered-audio verification")

    if private_unique_guard:
        unique_mode = "private_core_guard"
    elif bool(external_unique_guard.get("verified")):
        unique_mode = "external_selection_guard"
    else:
        unique_mode = "missing"

    payload = {
        "runtime_hardening": {
            **stats,
            "strict_unique_media": strict_unique,
            "strict_tts_chunk_order": strict_tts,
            "ordered_future_guard": strict_tts,
            "natural_filesystem_order_guard": strict_tts,
            "unique_media_enforcement": unique_mode,
            "external_unique_media": external_unique_guard,
            "verified": not errors,
        }
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if errors:
        raise SystemExit("Runtime hardening verification failed before TTS/render: " + "; ".join(errors))


def install_runtime_guards(core_dir: pathlib.Path, env: dict[str, str]) -> None:
    """Install deterministic TTS ordering and optional subprocess diagnostics."""
    guard_dir = core_dir / ".mediaforge-guards"
    guard_dir.mkdir(parents=True, exist_ok=True)
    diagnostics = enabled("MEDIAFORGE_DEBUG_SUBPROCESS_STDERR", False)
    strict_tts = enabled("MEDIAFORGE_TTS_STRICT_CHUNK_ORDER", True)

    guard_code = f'''import concurrent.futures as _cf
import glob as _glob
import os as _os
import pathlib as _pathlib
import re as _re
import subprocess as _subprocess
import sys as _sys


def _natural(value):
    name = getattr(value, "name", str(value))
    return tuple(int(piece) if piece.isdigit() else piece.lower() for piece in _re.split(r"(\\d+)", name))

_original_glob = _glob.glob
_original_listdir = _os.listdir
_original_path_glob = _pathlib.Path.glob
_original_path_rglob = _pathlib.Path.rglob


def _sorted_glob(*args, **kwargs):
    return sorted(_original_glob(*args, **kwargs), key=_natural)


def _sorted_listdir(*args, **kwargs):
    return sorted(_original_listdir(*args, **kwargs), key=_natural)


def _sorted_path_glob(self, pattern):
    return iter(sorted(_original_path_glob(self, pattern), key=_natural))


def _sorted_path_rglob(self, pattern):
    return iter(sorted(_original_path_rglob(self, pattern), key=_natural))

_glob.glob = _sorted_glob
_os.listdir = _sorted_listdir
_pathlib.Path.glob = _sorted_path_glob
_pathlib.Path.rglob = _sorted_path_rglob

_STRICT_TTS = {strict_tts!r}
if _STRICT_TTS:
    # Preserve submission order even if private TTS code uses as_completed(). This is
    # stronger than relying on filesystem ordering and directly prevents the observed
    # sentence/chunk swap around 00:56.
    def _ordered_as_completed(futures, timeout=None):
        ordered = list(futures)
        for future in ordered:
            future.result(timeout=timeout)
            yield future
    _cf.as_completed = _ordered_as_completed

    _OriginalThreadPoolExecutor = _cf.ThreadPoolExecutor
    class _SerialThreadPoolExecutor(_OriginalThreadPoolExecutor):
        def __init__(self, max_workers=None, *args, **kwargs):
            super().__init__(max_workers=1, *args, **kwargs)
    _cf.ThreadPoolExecutor = _SerialThreadPoolExecutor

    _OriginalProcessPoolExecutor = _cf.ProcessPoolExecutor
    class _SerialProcessPoolExecutor(_OriginalProcessPoolExecutor):
        def __init__(self, max_workers=None, *args, **kwargs):
            super().__init__(max_workers=1, *args, **kwargs)
    _cf.ProcessPoolExecutor = _SerialProcessPoolExecutor

_DIAGNOSTICS = {diagnostics!r}
_original_run = _subprocess.run

def _run(*args, **kwargs):
    try:
        return _original_run(*args, **kwargs)
    except _subprocess.CalledProcessError as exc:
        if _DIAGNOSTICS:
            captured = getattr(exc, "stderr", None)
            if captured:
                if isinstance(captured, bytes):
                    captured = captured.decode("utf-8", errors="replace")
                captured = str(captured)
                _sys.stderr.write("\\n--- MediaForge captured subprocess stderr ---\\n")
                _sys.stderr.write(captured)
                if not captured.endswith("\\n"):
                    _sys.stderr.write("\\n")
                _sys.stderr.write("--- End MediaForge captured subprocess stderr ---\\n")
        raise

_subprocess.run = _run
'''
    (guard_dir / "sitecustomize.py").write_text(guard_code, encoding="utf-8")
    previous = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = str(guard_dir) + (os.pathsep + previous if previous else "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=pathlib.Path)
    parser.add_argument("--job", required=True, type=pathlib.Path)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--locale", required=True)
    parser.add_argument("--output-dir", default="out", type=pathlib.Path)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Decrypt, harden and verify the private core without starting TTS/render",
    )
    args = parser.parse_args()

    bundle = args.bundle.resolve()
    job = args.job.resolve()
    output_dir = args.output_dir.resolve()

    if not bundle.is_file():
        raise SystemExit(f"Encrypted core bundle not found: {bundle}")
    if not job.is_file():
        raise SystemExit(f"Job manifest not found: {job}")

    if not args.verify_only:
        output_dir.mkdir(parents=True, exist_ok=True)
    plaintext = decrypt_bundle(bundle, load_key())

    with tempfile.TemporaryDirectory(prefix="mediaforge-core-") as tmp:
        core_dir = pathlib.Path(tmp)
        safe_extract_tar_gz(plaintext, core_dir)
        entrypoint = core_dir / "run.py"
        if not entrypoint.is_file():
            raise SystemExit("Encrypted core bundle must contain run.py")

        stats = apply_runtime_source_hardening(core_dir)
        verify_runtime_hardening(stats)
        if args.verify_only:
            print(json.dumps({"status": "private_core_preflight_ok"}))
            return

        env = os.environ.copy()
        env.update({
            "MEDIAFORGE_LANE": args.lane,
            "MEDIAFORGE_LOCALE": args.locale,
            "MEDIAFORGE_OUTPUT_DIR": str(output_dir),
            "MEDIAFORGE_JOB_PATH": str(job),
            "MEDIAFORGE_STRICT_UNIQUE_MEDIA": "1",
            "MEDIAFORGE_TTS_STRICT_CHUNK_ORDER": "1",
            "MEDIAFORGE_TTS_MAX_WORKERS": "1",
        })
        install_runtime_guards(core_dir, env)
        subprocess.run(["python", str(entrypoint)], check=True, cwd=core_dir, env=env)


if __name__ == "__main__":
    main()
