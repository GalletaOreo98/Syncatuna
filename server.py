#!/usr/bin/env python3
"""
Syncatuna - server.

Keeps the collaborative queue and a "master clock" of playback:
knows which song is playing, at which second, and from which server
timestamp.
It doesn't play audio, nor does it resolve metadata: each client does
that against its OWN yt-dlp before sending the "add". The server only
coordinates and validates what comes in.
"""
import re
import asyncio
import json
import signal
import sys
import time
import uuid
import logging
import math
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urlparse

import websockets
from websockets.asyncio.server import ServerConnection

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("syncatuna-server")

############## CONFIG VARS ##############
from config import load_config

CONFIG = load_config()
SERVER_CONFIG = CONFIG["server"]

HOST = str(
    SERVER_CONFIG["host"]
)

# bytes per websocket frame (messages are small, no need for more)
MAX_MSG_SIZE = int(
    SERVER_CONFIG["max_msg_size"]
)

WATCHDOG_INTERVAL = float(
    SERVER_CONFIG["watchdog_interval"]
)

AUTO_ADVANCE_GRACE = float(
    SERVER_CONFIG["auto_advance_grace"]
)

MANUAL_ADVANCE_GRACE = float(
    SERVER_CONFIG["manual_advance_grace"]
)

MAX_QUEUE_SIZE = int(
    SERVER_CONFIG["max_queue_size"]
)

MAX_URL_LENGTH = int(
    SERVER_CONFIG["max_url_length"]
)

MAX_TITLE_LENGTH = int(
    SERVER_CONFIG["max_title_length"]
)

MAX_NAME_LENGTH = int(
    SERVER_CONFIG["max_name_length"]
)

# Generous but not infinite xd
MAX_DURATION_SECONDS = int(
    SERVER_CONFIG["max_duration_seconds"]
)

ADD_COOLDOWN_SECONDS = float(
    SERVER_CONFIG["add_cooldown_seconds"]
)

last_add_time: dict = {}  #  to keep track of the "add" cooldown

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

# Control characters (including ESC, 0x1b Just in case xd)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_text(value, max_len: int) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL_CHARS_RE.sub("", value).strip()[:max_len]


def validate_url(url: str) -> bool:
    if len(url) > MAX_URL_LENGTH:
        return False

    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    hostname = (parsed.hostname or "").lower()

    if hostname not in ALLOWED_HOSTS:
        return False

    if parsed.username is not None or parsed.password is not None:
        return False

    return True


def validate_duration(value) -> "float | None":
    try:
        d = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(d) or d < 0 or d > MAX_DURATION_SECONDS:
        return None
    return d


@dataclass
class Track:
    id: str
    url: str
    title: str
    duration: float
    added_by: str


class Room:

    def __init__(self):
        self.queue: list[Track] = []
        self.current: Optional[Track] = None
        self.playing: bool = False
        self.anchor_position: float = 0.0   # second within the track at anchor_time
        self.anchor_time: float = time.time()
        self.clients: dict[ServerConnection, str] = {}
        self.advance_in_progress = False

    def live_position(self) -> float:
        if not self.current:
            return 0.0
        if not self.playing:
            return self.anchor_position
        return self.anchor_position + (time.time() - self.anchor_time)

    def set_anchor(self, position: float, playing: bool):
        self.anchor_position = position
        self.anchor_time = time.time()
        self.playing = playing

    def advance(self):
        if self.queue:
            self.current = self.queue.pop(0)
            self.set_anchor(0.0, True)
        else:
            self.current = None
            self.set_anchor(0.0, False)

    def state_message(self) -> dict:
        return {
            "type": "state",
            "queue": [asdict(t) for t in self.queue],
            "current": asdict(self.current) if self.current else None,
            "playing": self.playing,
            "anchor_position": self.anchor_position,
            "anchor_time": self.anchor_time,
            "server_time": time.time(),
            "users": list(self.clients.values()),
        }


room = Room()


async def broadcast():
    if not room.clients:
        return
    msg = json.dumps(room.state_message())
    stale = []
    for ws in list(room.clients.keys()):
        try:
            await ws.send(msg)
        except Exception:
            stale.append(ws)
    for ws in stale:
        room.clients.pop(ws, None)


async def advance_with_grace(grace: float, reason: str = "auto"):
    if room.advance_in_progress:
        return

    room.advance_in_progress = True

    try:
        room.advance()

        if room.current:
            room.set_anchor(0.0, False)

        await broadcast()

        track_id = room.current.id if room.current else None

        if track_id is None:
            return

        await asyncio.sleep(grace)

        if room.current and room.current.id == track_id and not room.playing:
            room.set_anchor(0.0, True)

            log.info(
                "Starting '%s' synced for everyone (%s, grace=%.1fs)",
                room.current.title,
                reason,
                grace,
            )

            await broadcast()

    finally:
        room.advance_in_progress = False


async def watchdog():
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)

        if room.current and room.playing and room.current.duration > 0:
            if room.live_position() >= room.current.duration:
                log.info(
                    "End of '%s' -> advancing queue (with sync margin)",
                    room.current.title,
                )

                await advance_with_grace(
                    AUTO_ADVANCE_GRACE,
                    reason="automatic",
                )


async def handler(ws: ServerConnection):
    name = f"user-{str(uuid.uuid4())[:4]}"
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")

            if mtype == "hello":
                name = sanitize_text(msg.get("name") or name, MAX_NAME_LENGTH) or name
                room.clients[ws] = name
                log.info("%s joined", name)
                await ws.send(json.dumps(room.state_message()))
                await broadcast()

            elif mtype == "add":
                url = (msg.get("url") or "").strip()
                if not url:
                    continue

                if len(room.queue) >= MAX_QUEUE_SIZE:
                    await ws.send(json.dumps({"type": "error", "message": "The queue is full."}))
                    continue

                now = time.time()
                if now - last_add_time.get(ws, 0.0) < ADD_COOLDOWN_SECONDS:
                    await ws.send(json.dumps({"type": "error", "message": "Wait a moment before adding another."}))
                    continue

                if not validate_url(url):
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Only YouTube URLs are allowed.",
                    }))
                    log.warning("URL rechazada: %r", url)
                    continue

                title = sanitize_text(msg.get("title"), MAX_TITLE_LENGTH) or url
                duration = validate_duration(msg.get("duration"))
                if duration is None:
                    await ws.send(json.dumps({"type": "error", "message": "Invalid duration."}))
                    continue

                last_add_time[ws] = now
                track = Track(
                    id=str(uuid.uuid4())[:8], url=url, title=title,
                    duration=duration,
                    added_by=room.clients.get(ws, name),
                )
                room.queue.append(track)
                if room.current is None:
                    room.advance()
                log.info("%s added: %s (%.0fs)", track.added_by, track.title, track.duration)
                await broadcast()

            elif mtype == "next":
                who = room.clients.get(ws, name)

                if room.advance_in_progress:
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Wait for the current track change to finish."
                    }))
                    log.info("%s tried to do next during the grace", who)
                    continue

                log.info("%s requested next", who)

                await advance_with_grace(
                    MANUAL_ADVANCE_GRACE,
                    reason=f"manual by {who}",
                )

            elif mtype == "pause":
                if room.current:
                    room.set_anchor(room.live_position(), False)
                    await broadcast()

            elif mtype == "resume":
                if room.current:
                    room.set_anchor(room.live_position(), True)
                    await broadcast()

            elif mtype == "seek":
                try:
                    pos = float(msg.get("position", 0))
                except (TypeError, ValueError):
                    continue
                if room.current:
                    room.set_anchor(max(0.0, pos), room.playing)
                    await broadcast()

            elif mtype == "ping":
                await ws.send(json.dumps({"type": "pong", "t0": msg.get("t0"), "server_time": time.time()}))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        last_add_time.pop(ws, None)
        left = room.clients.pop(ws, None)
        if left:
            log.info("%s left", left)
            await broadcast()


async def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    port = int(args[0]) if args else int(
        SERVER_CONFIG["port"]
    )
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    # SIGHUP arrives when closing the terminal/tab, SIGTERM on a normal kill
    # SIGINT is Ctrl+C. With all 3 covered no zombie process is left
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, stop.set)

    async with websockets.serve(handler, HOST, port, ping_interval=20, ping_timeout=20, max_size=MAX_MSG_SIZE):
        log.info("Syncatuna server listening on %s:%s (use your VPN IP so clients can connect)", HOST, port)
        watchdog_task = asyncio.create_task(watchdog())
        await stop.wait()
        log.info("Shutting down server...")
        watchdog_task.cancel()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass