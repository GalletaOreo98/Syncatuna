#!/usr/bin/env python3
"""
Syncatuna - favorites.

Reads, writes and extends the client's local favorites file
(~/.config/syncatuna/favorites.txt). Each line holds one track, either
as a plain URL (legacy format) or as "url - title". Lines are deduplicated
by URL (not by raw text).
"""
import random
import re
import subprocess

from config import get_config_dir

FAVORITES_FILE = get_config_dir() / "favorites.txt"

# The URL part is a non-space token, so " - " is a safe separator even when
# the title itself contains " - " (e.g. "Artist - Song - Remix").
_URL_TITLE_RE = re.compile(r"^(https?://\S+?)(?: - (.*))?$")


def parse_line(text: str) -> "tuple[str, str] | None":
    line = text.strip()
    if not line or line.startswith("#"):
        return None
    match = _URL_TITLE_RE.match(line)
    if not match:
        return None
    url = match.group(1)
    title = (match.group(2) or "").strip()
    return (url, title)


def load_favorites() -> "list[tuple[str, str]]":
    try:
        lines = FAVORITES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in lines:
        parsed = parse_line(line)
        if parsed is None:
            continue
        url, title = parsed
        if url in seen:
            continue
        seen.add(url)
        entries.append((url, title))
    return entries


def count_favorites() -> int:
    return len(load_favorites())


def format_line(url: str, title: str) -> str:
    return f"{url} - {title}" if title else url


def add_from_playlist(playlist_url: str) -> dict:
    try:
        result = subprocess.run(
            [
                "yt-dlp", "--flat-playlist", "--no-warnings",
                "--print", "%(url)s - %(title)s", "--", playlist_url,
            ],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not run yt-dlp: {exc}") from exc

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:500] or "yt-dlp failed")

    try:
        raw_lines = FAVORITES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        raw_lines = []
    comments = [line.strip() for line in raw_lines if line.strip().startswith("#")]

    # key: url -> title
    merged: dict[str, str] = {}
    for url, title in load_favorites():
        merged.setdefault(url, title)

    added = 0
    fetched = 0
    for line in result.stdout.splitlines():
        parsed = parse_line(line)
        if parsed is None:
            continue
        url, title = parsed
        fetched += 1
        if url not in merged:
            merged[url] = title
            added += 1
        elif title and not merged[url]:
            merged[url] = title

    entries = sorted((u, t) for u, t in merged.items())
    content = "\n".join(format_line(u, t) for u, t in entries)
    if comments:
        content = "\n".join(comments) + "\n\n" + content
    if content:
        content += "\n"

    FAVORITES_FILE.parent.mkdir(parents=True, exist_ok=True)
    FAVORITES_FILE.write_text(content, encoding="utf-8")

    return {"added": added, "fetched": fetched, "total": len(entries)}


def pick_random_favorite(attempted: "set[str] | None" = None) -> "tuple[str, str] | None":
    attempted = attempted or set()
    pool = [entry for entry in load_favorites() if entry[0] not in attempted]
    if not pool:
        return None
    return random.choice(pool)