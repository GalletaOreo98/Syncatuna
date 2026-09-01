#!/usr/bin/env python3
"""
Syncatuna - servidor.

Mantiene la cola colaborativa y un "reloj maestro" de reproduccion:
sabe qué cancion suena, en qué segundo, y desde qué instante del
servidor. 
No reproduce audio: solo coordina a los clientes.

"""
import os
import asyncio
import json
import signal
import sys
import time
import uuid
import subprocess
import logging
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urlparse
from pathlib import Path

import websockets
from websockets.asyncio.server import ServerConnection

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("syncatuna-server")

############## CONFIG VARS ##############

HOST = "127.0.0.1"
WATCHDOG_INTERVAL = 2.0    # cada cuánto revisa si ya terminó la canción actual
AUTO_ADVANCE_GRACE = 3.0

MAX_QUEUE_SIZE = 100
MAX_URL_LENGTH = 2048

METADATA_CONCURRENCY = 2
metadata_semaphore = asyncio.Semaphore(METADATA_CONCURRENCY)

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

YT_DLP_BASE_ARGS = [
    "yt-dlp",
    "--ignore-config",
    "-J",
    "--no-warnings",
    "--skip-download",
    "--no-playlist",
]

COOKIES_FILE = Path(
    os.environ.get(
        "SYNCATUNA_COOKIES",
        "~/.config/syncatuna/cookies.txt",
    )
)


def fetch_metadata(url: str) -> dict:
    args = YT_DLP_BASE_ARGS.copy()

    if COOKIES_FILE.is_file():
        args += ["--cookies", str(COOKIES_FILE)]

    args += ["--", url]
    
    try:
        result = subprocess.run(
            args,
            capture_output=True, 
            text=True, 
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[:300])
        data = json.loads(result.stdout)
        return {
            "title": data.get("title") or url,
            "duration": float(data.get("duration") or 0),
            #"thumbnail": data.get("thumbnail") or "",
        }
    except Exception as e:
        log.warning("No se pudo obtener metadata de %s: %s", url, e)
        return {"title": url, "duration": 0.0}
        #return {"title": url, "duration": 0.0, "thumbnail": ""}


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


@dataclass
class Track:
    id: str
    url: str
    title: str
    duration: float
    #thumbnail: str
    added_by: str


class Room:

    def __init__(self):
        self.queue: list[Track] = []
        self.current: Optional[Track] = None
        self.playing: bool = False
        self.anchor_position: float = 0.0   # segundo del track en anchor_time
        self.anchor_time: float = time.time()
        self.clients: dict[ServerConnection, str] = {}

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


async def advance_with_grace():
    room.advance()
    if room.current:
        room.set_anchor(0.0, False)  # la dejamos cargada pero en pausa
    await broadcast()

    track_id = room.current.id if room.current else None
    if track_id is None:
        return

    await asyncio.sleep(AUTO_ADVANCE_GRACE)

    if room.current and room.current.id == track_id and not room.playing:
        room.set_anchor(0.0, True)
        log.info("Arrancando '%s' sincronizado para todos", room.current.title)
        await broadcast()


async def watchdog():
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        if room.current and room.playing and room.current.duration > 0:
            if room.live_position() >= room.current.duration:
                log.info("Fin de '%s' -> avanzando cola (con margen de sincronización)", room.current.title)
                await advance_with_grace()


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
                name = (msg.get("name") or name)[:24]
                room.clients[ws] = name
                log.info("%s se unió", name)
                await ws.send(json.dumps(room.state_message()))
                await broadcast()

            elif mtype == "add":
                url = (msg.get("url") or "").strip()
                if not url:
                    continue
                if len(room.queue) >= MAX_QUEUE_SIZE:
                    log.warning("Cola llena; rechazando nueva URL")
                    continue
                if not validate_url(url):
                    log.warning("URL rechazada: %r", url)
                    continue
                #meta = await asyncio.to_thread(fetch_metadata, url)
                # Es como un semaforo para que no se sobrecargue de solicitudes de descarga de metadata al mismo tiempo
                async with metadata_semaphore:
                    meta = await asyncio.to_thread(fetch_metadata, url)
                track = Track(
                    id=str(uuid.uuid4())[:8], url=url, title=meta["title"],
                    #duration=meta["duration"], thumbnail=meta["thumbnail"],
                    duration=meta["duration"],
                    added_by=room.clients.get(ws, name),
                )
                room.queue.append(track)
                if room.current is None:
                    room.advance()
                log.info("%s agregó: %s (%.0fs)", track.added_by, track.title, track.duration)
                await broadcast()

            elif mtype == "next":
                room.advance()
                who = room.clients.get(ws, name)
                what = room.current.title if room.current else "(cola vacía)"
                log.info("%s pidió next -> %s", who, what)
                await broadcast()

            elif mtype == "pause":
                if room.current:
                    room.set_anchor(room.live_position(), False)
                    await broadcast()

            elif mtype == "resume":
                if room.current:
                    room.set_anchor(room.live_position(), True)
                    await broadcast()

            elif mtype == "seek":
                pos = float(msg.get("position", 0))
                if room.current:
                    room.set_anchor(max(0.0, pos), room.playing)
                    await broadcast()

            elif mtype == "ping":
                await ws.send(json.dumps({"type": "pong", "t0": msg.get("t0"), "server_time": time.time()}))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        left = room.clients.pop(ws, None)
        if left:
            log.info("%s se fue", left)
            await broadcast()


async def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    port = int(args[0]) if args else 8765
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    # SIGHUP llega al cerrar la terminal/pestaña, SIGTERM en un kill normal
    # SIGINT es Ctrl+C. Con los 3 cubiertos no queda el proceso zombie
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, stop.set)

    async with websockets.serve(handler, HOST, port, ping_interval=20, ping_timeout=20):
        log.info("Servidor Syncatuna escuchando en %s:%s (usa tu IP de VPN para que se conecten)", HOST, port)
        log.info("Cookies file path: %s" , COOKIES_FILE)
        watchdog_task = asyncio.create_task(watchdog())
        await stop.wait()
        log.info("Cerrando servidor...")
        watchdog_task.cancel()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
