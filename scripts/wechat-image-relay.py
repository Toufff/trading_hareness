#!/usr/bin/env python3
"""Relay newly-created local WeChat media files to the existing n8n import path.

This watches local files only. It does not inspect WeChat databases, cookies,
network traffic, or message contents.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request


DEFAULT_ROOT = Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
STATE_DIR = Path("/Users/papa/codebase/n8n/state")
DEFAULT_STATE = STATE_DIR / "wechat-image-relay-seen.json"
SUPPORTED_TAGS = {"liwei", "liuzi", "xiaolan"}
MAX_FILE_BYTES = 12 * 1024 * 1024
MEDIA_MAGIC = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"ID3": "audio/mpeg",
}
MEDIA_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
}


def log(message: str) -> None:
    print(f"{dt.datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


def media_type_for(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return None
    for magic, media_type in MEDIA_MAGIC.items():
        if head.startswith(magic):
            return media_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in {b"M4A ", b"mp42", b"isom", b"iso2"} and path.suffix.lower() in {".m4a", ".aac"}:
            return "audio/mp4"
        return "video/mp4"
    return None


def discover_watch_dirs(root: Path, include_rwtemp: bool, chat_dir_ids: set[str]) -> list[Path]:
    dirs: list[Path] = []
    if not root.exists():
        return dirs
    for account in root.iterdir():
        if not account.is_dir():
            continue
        if chat_dir_ids:
            for chat_dir_id in chat_dir_ids:
                for base in [account / "msg/attach" / chat_dir_id, account / "temp" / chat_dir_id]:
                    if base.is_dir():
                        dirs.append(base)
            continue
        input_temp = account / "temp/InputTemp"
        if input_temp.is_dir():
            dirs.append(input_temp)
        if include_rwtemp:
            month = dt.datetime.now().strftime("%Y-%m")
            rwtemp = account / "temp/RWTemp" / month
            if rwtemp.is_dir():
                dirs.append(rwtemp)
    return sorted(set(dirs))


def is_candidate_media_path(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith("_t.dat") or name.endswith("_t"):
        return False
    return True


def iter_media(dirs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for directory in dirs:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and is_candidate_media_path(path):
                paths.append(path)
    return paths


def stable_stat(path: Path, stable_seconds: float) -> os.stat_result | None:
    try:
        first = path.stat()
    except OSError:
        return None
    if first.st_size <= 0 or first.st_size > MAX_FILE_BYTES:
        return None
    if time.time() - first.st_mtime < stable_seconds:
        return None
    time.sleep(0.15)
    try:
        second = path.stat()
    except OSError:
        return None
    if first.st_size != second.st_size or first.st_mtime != second.st_mtime:
        return None
    return second


def load_seen(path: Path) -> set[str]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, list):
        return set()
    return {item for item in payload if isinstance(item, str)}


def save_seen(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(seen)[-5000:], ensure_ascii=False, indent=2) + "\n")


def file_payload(path: Path, stat: os.stat_result) -> dict[str, object] | None:
    media_type = media_type_for(path)
    if media_type is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()
    extension = MEDIA_EXTENSIONS.get(media_type, "bin")
    filename = path.name
    if path.suffix.lower() in {"", ".dat"}:
        filename = f"{path.stem}.{extension}"
    return {
        "path": str(path),
        "sha256": digest,
        "filename": filename,
        "media_type": media_type,
        "data_base64": base64.b64encode(data).decode("ascii"),
        "mtime": stat.st_mtime,
    }


def content_datetime(timestamp: float) -> tuple[str, str]:
    # Keep the target business record aligned with the existing Feishu import
    # convention: content_date/content_time are recorded in Asia/Shanghai.
    zone = dt.timezone(dt.timedelta(hours=8))
    value = dt.datetime.fromtimestamp(timestamp, tz=zone)
    return value.strftime("%Y-%m-%d"), value.strftime("%H:%M")


def post_batch(
    endpoint: str,
    tag: str,
    source_label: str,
    text: str,
    files: list[dict[str, object]],
    dry_run: bool,
) -> bool:
    if not files:
        return True
    latest_mtime = max(float(item["mtime"]) for item in files)
    content_date, content_time = content_datetime(latest_mtime)
    body = {
        "tag": tag,
        "text": text,
        "ingress_source": "wechat-image-watcher",
        "source_label": source_label,
        "content_date": content_date,
        "content_time": content_time,
        "media": [
            {
                "filename": item["filename"],
                "media_type": item["media_type"],
                "data_base64": item["data_base64"],
            }
            for item in files
        ],
    }
    paths = ", ".join(str(item["path"]) for item in files)
    if dry_run:
        log(f"DRY RUN would relay {len(files)} image(s) as #{tag}: {paths}")
        return True

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            reply = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        log(f"relay failed HTTP {error.code}: {detail}")
        return False
    except OSError as error:
        log(f"relay failed: {error}")
        return False
    log(f"relayed {len(files)} image(s) as #{tag}: {reply}")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch local WeChat media temp files and relay them to n8n.")
    parser.add_argument("--tag", required=True, choices=sorted(SUPPORTED_TAGS), help="route tag used by the market import workflow")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="WeChat xwechat_files root")
    parser.add_argument("--endpoint", default="http://127.0.0.1:5680/manual-relay", help="local feishu-adapter manual relay endpoint")
    parser.add_argument("--source-label", default="微信本机图片监控", help="source label stored with the imported record")
    parser.add_argument("--text", default="", help="optional text to attach to each image batch")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="dedupe state file")
    parser.add_argument("--poll", type=float, default=1.0, help="poll interval in seconds")
    parser.add_argument("--stable", type=float, default=1.0, help="seconds a file must remain unchanged before upload")
    parser.add_argument("--batch-window", type=float, default=2.0, help="seconds to collect nearby images into one post")
    parser.add_argument("--chat-dir-id", action="append", default=[], help="only watch this WeChat per-chat media directory id; repeatable")
    parser.add_argument("--include-rwtemp", action="store_true", help="also watch current-month temp/RWTemp; broader and more error-prone")
    parser.add_argument("--dry-run", action="store_true", help="print candidate files without posting")
    parser.add_argument("--once", action="store_true", help="scan once and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    start_time = time.time()
    seen = load_seen(args.state)
    pending: list[dict[str, object]] = []
    last_added = 0.0

    chat_dir_ids = {str(value).strip() for value in args.chat_dir_id if str(value).strip()}
    watch_dirs = discover_watch_dirs(args.root, args.include_rwtemp, chat_dir_ids)
    if not watch_dirs:
        log(f"no WeChat watch directories found under {args.root}")
        return 2
    for directory in watch_dirs:
        log(f"watching {directory}")
    log(f"route=#{args.tag} endpoint={args.endpoint} dry_run={args.dry_run}")

    while True:
        for path in iter_media(watch_dirs):
            try:
                initial_stat = path.stat()
            except OSError:
                continue
            if initial_stat.st_mtime < start_time:
                continue
            stat = stable_stat(path, args.stable)
            if stat is None:
                continue
            item = file_payload(path, stat)
            if item is None:
                continue
            digest = str(item["sha256"])
            if digest in seen or any(str(existing["sha256"]) == digest for existing in pending):
                continue
            pending.append(item)
            last_added = time.time()
            log(f"candidate {path}")

        if pending and time.time() - last_added >= args.batch_window:
            batch = pending[:12]
            if post_batch(args.endpoint, args.tag, args.source_label, args.text, batch, args.dry_run):
                seen.update(str(item["sha256"]) for item in batch)
                save_seen(args.state, seen)
                del pending[: len(batch)]

        if args.once:
            break
        time.sleep(args.poll)
    return 0


if __name__ == "__main__":
    sys.exit(main())
