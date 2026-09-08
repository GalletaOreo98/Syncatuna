#!/usr/bin/env python3
"""
Syncatuna - client.
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

import websockets

try:
    from prompt_toolkit import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout.containers import HSplit, VSplit, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.layout.layout import Layout
    from prompt_toolkit.patch_stdout import patch_stdout
except ImportError:
    print("Missing prompt_toolkit. Install it with: pip install --user prompt_toolkit")
    sys.exit(1)

############## CONFIG VARS ##############
from config import load_config

from favorites import count_favorites, pick_random_favorite

CONFIG = load_config()
CLIENT_CONFIG = CONFIG["client"]

CLOCK_SYNC_INTERVAL = float(
    CLIENT_CONFIG["clock_sync_interval"]
)

# (bottom toolbar)
TICKER_INTERVAL = float(
    CLIENT_CONFIG["ticker_interval"]
)

MAX_URL_TRIES = int(
    CLIENT_CONFIG["autofill"]["max_url_tries"]
)

_app: "Application | None" = None

MESSAGE_TIMEOUT = 8.0
ANNOUNCEMENT: "list[float | str | None]" = [None, None]
STATUS_MESSAGE: "list[float | str | None]" = [None, None, "yellow"]


def invalidate_ui():
    if _app is not None:
        try:
            _app.invalidate()
        except Exception:
            pass


def emit(text: str, color: str = "yellow"):
    now = time.time()
    STATUS_MESSAGE[0] = now
    STATUS_MESSAGE[1] = text
    STATUS_MESSAGE[2] = color
    invalidate_ui()


def announce(text: str):
    now = time.time()
    ANNOUNCEMENT[0] = now
    ANNOUNCEMENT[1] = text
    invalidate_ui()


def fetch_metadata_local(url: str, quiet: bool = False) -> dict:
    try:
        result = subprocess.run(
            ["yt-dlp", "-J", "--no-warnings", "--skip-download", "--no-playlist", "--", url],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[:300])
        data = json.loads(result.stdout)
        return {"title": data.get("title") or url, "duration": float(data.get("duration") or 0)}
    except Exception as e:
        if not quiet:
            emit(f"Could not read metadata from that URL with yt-dlp: {e}")
        return None


class MPV:

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.proc = None
        self.sock: "socket.socket | None" = None
        self._req_id = 0

    def start(self):
        self.proc = subprocess.Popen(
            [
                "mpv", "--no-video", "--idle=yes", "--no-terminal",
                f"--input-ipc-server={self.socket_path}",
                "--ytdl-format=bestaudio/best",
                "--script-opts=ytdl_hook-ytdl_path=yt-dlp",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if os.path.exists(self.socket_path):
                try:
                    self._connect()
                    return
                except OSError:
                    pass
            time.sleep(0.1)
        raise RuntimeError("mpv didn't start the IPC socket in time (is mpv installed?)")

    def _connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)
        self.sock.settimeout(3.0)

    def _send(self, command: list) -> dict:
        self._req_id += 1
        payload = json.dumps({"command": command, "request_id": self._req_id}) + "\n"
        try:
            self.sock.sendall(payload.encode())
            buf = b""
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                for line in buf.split(b"\n"):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("request_id") == self._req_id:
                        return obj
                buf = b""
        except (OSError, socket.timeout):
            pass
        return {}

    def loadfile(self, url: str):
        self._send(["loadfile", url, "replace"])

    def set_pause(self, paused: bool):
        self._send(["set_property", "pause", paused])

    def seek_abs(self, position: float):
        self._send(["set_property", "time-pos", max(0.0, position)])

    def get_time_pos(self) -> "float | None":
        r = self._send(["get_property", "time-pos"])
        data = r.get("data")
        return float(data) if data is not None else None

    def is_ready(self) -> bool:
        return self.get_time_pos() is not None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class ClientState:
    def __init__(self):
        self.queue = []
        self.current = None
        self.playing = False
        self.anchor_position = 0.0
        self.anchor_time = 0.0
        self.users = []
        self.clock_offset = 0.0
        self.loaded_track_id = "__none__"
        self.loading_task = None

    def server_now(self) -> float:
        return time.time() + self.clock_offset

    def expected_position(self) -> float:
        if not self.current:
            return 0.0
        if not self.playing:
            return self.anchor_position
        return self.anchor_position + (self.server_now() - self.anchor_time)


state = ClientState()


def handle_pong(msg):
    t0 = msg.get("t0")
    server_time = msg.get("server_time")
    if t0 is None or server_time is None:
        return
    t1 = time.time()
    rtt = t1 - t0
    state.clock_offset = server_time - (t0 + rtt / 2)


async def clock_sync_loop(ws):
    while True:
        await ws.send(json.dumps({"type": "ping", "t0": time.time()}))
        await asyncio.sleep(CLOCK_SYNC_INTERVAL)


async def apply_new_track(mpv: MPV, track_id: str, url: str):
    try:
        await asyncio.to_thread(mpv.loadfile, url)

        ready = False

        for _ in range(100):
            await asyncio.sleep(0.15)

            # If the song changed while loading, we abort
            if not state.current or state.current["id"] != track_id:
                return

            ready = await asyncio.to_thread(mpv.is_ready)

            if ready:
                break

        if not ready:
            emit("Could not load the song in time")
            return

        if not state.current or state.current["id"] != track_id:
            return

        state.loaded_track_id = track_id

        live_target = state.expected_position()

        await asyncio.to_thread(mpv.seek_abs, live_target)
        await asyncio.to_thread(mpv.set_pause, not state.playing)

    except asyncio.CancelledError:
        return


def dashboard_width() -> int:
    import shutil
    columns = 0
    if _app is not None:
        try:
            columns = _app.output.get_size().columns
        except Exception:
            columns = 0
    if columns <= 0:
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    return max(20, min(columns, 110))


def render_dashboard() -> str:
    import io as _io
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.text import Text

    from rich.cells import cell_len

    width = dashboard_width()

    left = f"🟢 Connected ({len(state.users)})"
    right = f"⭐ {count_favorites()}"
    # Keep the row below the panel's wrap threshold: a line that lands exactly
    # on the interior edge with a wide char folds its last cell by the panel.
    pad = max(1, width - 4 - cell_len(left) - cell_len(right))
    connected = Text()
    connected.append(left)
    connected.append(" " * pad)
    connected.append(right)

    now_playing = Text()
    if state.current:
        now_playing.append("🎵 ", style="bold")
        now_playing.append(state.current["title"], style="bold")
        now_playing.append("\n")
        now_playing.append("added by ", style="dim")
        now_playing.append(state.current["added_by"], style="dim")
        now_playing.append(" · lasts ", style="dim")
        now_playing.append(fmt_time(state.current["duration"]), style="dim")
    else:
        now_playing.append("(nothing playing - paste a YouTube URL and press Enter)", style="dim")

    shown = state.queue[:2]
    remaining = len(state.queue) - len(shown)
    queue_text = Text()
    queue_text.append("Queue\n", style="bold")
    if shown:
        for i, t in enumerate(shown):
            queue_text.append(f"{i + 1}. ")
            queue_text.append(t["title"])
            queue_text.append(f"  (+{t['added_by']})", style="dim")
            if i < len(shown) - 1 or remaining > 0:
                queue_text.append("\n")
        if remaining > 0:
            queue_text.append(f"+{remaining}", style="dim")
    else:
        queue_text.append("(empty)", style="dim")

    group = Group(connected, now_playing, Rule(style="dim"), queue_text)

    buf = _io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="256", width=width)

    now = time.time()
    if ANNOUNCEMENT[1] is not None and now - float(ANNOUNCEMENT[0]) <= MESSAGE_TIMEOUT:
        console.print(Text(f"+ {ANNOUNCEMENT[1]}", style="green"), overflow="fold")
        console.print()

    console.print(Panel(
        group,
        title="🐱 Syncatuna 🐱",
        subtitle="URL=add · n=next · p=pause · r=resume · q=quit",
        subtitle_align="center",
        width=width,
    ))
    return buf.getvalue()


async def receiver(ws, mpv: MPV):
    async for raw in ws:
        msg = json.loads(raw)
        mtype = msg.get("type")
        if mtype == "pong":
            handle_pong(msg)
        elif mtype == "state":
            await apply_state(msg, mpv)
        elif mtype == "error":
            emit(f"{msg.get('message', 'Server error.')}")
        elif mtype == "autofill_request":
            request_id = msg.get("request_id")
            if request_id is None:
                continue
            payload = {"type": "autofill_result", "request_id": request_id, "ok": False}
            attempted: set[str] = set()
            for _ in range(MAX_URL_TRIES):
                entry = pick_random_favorite(attempted)
                if entry is None:
                    break
                url, _title = entry
                attempted.add(url)
                meta = await asyncio.to_thread(fetch_metadata_local, url, True)
                if meta is not None:
                    payload = {
                        "type": "autofill_result", "request_id": request_id,
                        "ok": True, "url": url,
                        "title": meta["title"], "duration": meta["duration"],
                    }
                    break
            await ws.send(json.dumps(payload))


async def apply_state(msg, mpv: MPV):
    old_queue_ids = {t["id"] for t in state.queue}
    old_known_ids = old_queue_ids | ({state.current["id"]} if state.current else set())
    old_users = set(state.users)

    state.queue = msg["queue"]
    new_current = msg["current"]
    state.playing = msg["playing"]
    state.anchor_position = msg["anchor_position"]
    state.anchor_time = msg["anchor_time"]
    state.users = msg["users"]

    new_queue_ids = {t["id"] for t in state.queue}
    new_known_ids = new_queue_ids | ({new_current["id"]} if new_current else set())
    queue_changed = new_queue_ids != old_queue_ids
    users_changed = set(state.users) != old_users
    added_ids = new_known_ids - old_known_ids

    new_id = new_current["id"] if new_current else "__none__"
    track_changed = new_id != state.loaded_track_id

    if track_changed:
        # Cancel the previous load if it still exists.
        if state.loading_task and not state.loading_task.done():
            state.loading_task.cancel()

        state.current = new_current
        state.loaded_track_id = "__none__"

        if new_current:
            state.loading_task = asyncio.create_task(
                apply_new_track(
                    mpv,
                    new_id,
                    new_current["url"],
                )
            )
        else:
            await asyncio.to_thread(mpv.set_pause, True)

    else:
        state.current = new_current

        # If the track is still loading, DON'T attempt
        # seek/pause on it yet.
        if state.loaded_track_id == new_id:
            await asyncio.to_thread(
                mpv.seek_abs,
                state.expected_position()
            )

            await asyncio.to_thread(
                mpv.set_pause,
                not state.playing
            )

    if added_ids:
        all_known = list(state.queue) + ([state.current] if state.current else [])
        added_tracks = [t for t in all_known if t["id"] in added_ids]
        for t in added_tracks:
            announce(f"{t['added_by']} added: {t['title']}")
    invalidate_ui()


def fmt_time(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


def bottom_toolbar():
    if not state.current:
        return " (Nothing playing yet — paste a YouTube URL and press Enter)"
    status = "▶" if state.playing else "⏸"
    pos = fmt_time(state.expected_position())
    dur = fmt_time(state.current["duration"])
    title = state.current["title"]
    if len(title) > 60:
        title = title[:57] + "..."
    return f" {status} {title}  |  {pos} / {dur}"


def print_help():
    emit("URL=add · n=next · p=pause · r=resume · q=quit · paste a URL to add it")


async def handle_command(line: str, ws):
    line = line.strip()
    if not line:
        return False
    if line in ("q", "quit"):
        return True
    elif line in ("n", "next"):
        await ws.send(json.dumps({"type": "next"}))
    elif line in ("p", "pause"):
        await ws.send(json.dumps({"type": "pause"}))
    elif line in ("r", "resume", "play"):
        await ws.send(json.dumps({"type": "resume"}))
    elif line in ("h", "help", "?"):
        print_help()
    elif line.startswith("http"):
        emit("… resolving with yt-dlp")
        meta = await asyncio.to_thread(fetch_metadata_local, line)
        if meta is not None:
            await ws.send(json.dumps({
                "type": "add", "url": line,
                "title": meta["title"], "duration": meta["duration"],
            }))
    else:
        emit("Unrecognized command. Type 'h' for help.", color="red")
    return False


def dashboard_body():
    try:
        return ANSI(render_dashboard())
    except Exception:
        return "dashboard error"


def status_body():
    if STATUS_MESSAGE[1] is None or time.time() - float(STATUS_MESSAGE[0]) > MESSAGE_TIMEOUT:
        return ""
    color = "\x1b[31m" if STATUS_MESSAGE[2] == "red" else "\x1b[33m"
    text = str(STATUS_MESSAGE[1])
    return ANSI(f"{color}{text}\x1b[0m")


def build_app(ws) -> "Application":
    kb = KeyBindings()

    @kb.add("enter")
    async def _on_enter(event):
        text = event.current_buffer.text
        event.current_buffer.reset()
        if await handle_command(text, ws):
            event.app.exit()

    @kb.add("c-c")
    @kb.add("c-d")
    async def _on_quit(event):
        event.app.exit()

    buffer = Buffer()

    layout = Layout(HSplit([
        Window(FormattedTextControl(dashboard_body), always_hide_cursor=True),
        Window(FormattedTextControl(status_body), height=1, always_hide_cursor=True),
        VSplit([
            Window(FormattedTextControl("> "), width=2, height=1,
                   dont_extend_width=True),
            Window(BufferControl(buffer=buffer), height=1),
        ]),
        Window(FormattedTextControl(bottom_toolbar), height=1, style="reverse"),
    ]))

    return Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        erase_when_done=True,
    )


async def ticker(app):
    try:
        while True:
            await asyncio.sleep(TICKER_INTERVAL)
            app.invalidate()
    except asyncio.CancelledError:
        pass


def install_shutdown_handlers(mpv: MPV):

    def _shutdown(signum, _frame):
        mpv.stop()
        os._exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _shutdown)


async def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print('Internal usage: client.py ws://HOST_IP:8765 "YourName"')
        return 2
    url, name = args[0], args[1]

    socket_path = os.path.join(tempfile.gettempdir(), f"syncatuna-mpv-{os.getpid()}.sock")
    mpv = MPV(socket_path)
    mpv.start()
    install_shutdown_handlers(mpv)

    global _app
    try:
        with patch_stdout():
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"type": "hello", "name": name,
                                          "favorites_count": count_favorites()}))
                app = build_app(ws)
                _app = app
                ticker_task = asyncio.create_task(ticker(app))
                asyncio.create_task(clock_sync_loop(ws))
                recv_task = asyncio.create_task(receiver(ws, mpv))
                try:
                    await app.run_async()
                finally:
                    recv_task.cancel()
                    ticker_task.cancel()
    finally:
        _app = None
        mpv.stop()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass