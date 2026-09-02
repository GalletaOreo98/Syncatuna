#!/usr/bin/env python3
"""
Syncatuna - cliente.
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
    from prompt_toolkit import PromptSession, print_formatted_text
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.formatted_text import ANSI
except ImportError:
    print("Falta prompt_toolkit. Instálalo con: pip install --user prompt_toolkit")
    sys.exit(1)

CLOCK_SYNC_INTERVAL = 20.0 
TICKER_INTERVAL = 1.0 #(barra inferior)

session = PromptSession()


def emit(text: str):
    print_formatted_text(ANSI(text))


def fetch_metadata_local(url: str) -> dict:
    """
    Resuelve título y duración con NUESTRO propio yt-dlp, antes de mandar
    el "add" al servidor. El servidor no ejecuta yt-dlp
    """
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
        emit(f"No pude leer los datos de esa URL con yt-dlp: {e}")
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
        raise RuntimeError("mpv no levantó el socket IPC a tiempo (¿está instalado mpv?)")

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
    """Carga una pista y, cuando mpv esté listo, aplica el estado ACTUAL."""
    try:
        await asyncio.to_thread(mpv.loadfile, url)

        ready = False

        for _ in range(100):
            await asyncio.sleep(0.15)

            # Si mientras cargábamos cambió la canción, abandonamos
            if not state.current or state.current["id"] != track_id:
                return

            ready = await asyncio.to_thread(mpv.is_ready)

            if ready:
                break

        if not ready:
            emit("No se pudo cargar la canción a tiempo")
            return

        # MUY IMPORTANTE:
        # No usamos should_play ni target_position guardados del pasado.
        # Consultamos el estado actual del servidor.
        if not state.current or state.current["id"] != track_id:
            return

        state.loaded_track_id = track_id

        live_target = state.expected_position()

        await asyncio.to_thread(mpv.seek_abs, live_target)
        await asyncio.to_thread(mpv.set_pause, not state.playing)

    except asyncio.CancelledError:
        return


DASHBOARD_WIDTH = 62


def render_dashboard(announcements: "list[str] | None" = None, clear: bool = False) -> str:
    import io as _io
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.text import Text

    conectados = Text(f"🟢 Conectados ({len(state.users)})")

    now_playing = Text()
    if state.current:
        now_playing.append("🎵 ", style="bold")
        now_playing.append(state.current["title"], style="bold")
        now_playing.append("\n")
        now_playing.append("agregado por ", style="dim")
        now_playing.append(state.current["added_by"], style="dim")
        now_playing.append(" · dura ", style="dim")
        now_playing.append(fmt_time(state.current["duration"]), style="dim")
    else:
        now_playing.append("(nada sonando - pega una URL de YouTube y Enter)", style="dim")

    shown = state.queue[:2]
    remaining = len(state.queue) - len(shown)
    queue_text = Text()
    queue_text.append("Cola\n", style="bold")
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
        queue_text.append("(vacía)", style="dim")

    group = Group(conectados, now_playing, Rule(style="dim"), queue_text)

    buf = _io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="256", width=DASHBOARD_WIDTH)

    if clear:
        import shutil
        term_lines = shutil.get_terminal_size(fallback=(80, 24)).lines
        console.print("\n" * term_lines, end="")
    if announcements:
        for line in announcements:
            console.print(Text(f"+ {line}", style="green"))
        console.print()  # linea en blanco entre los avisos y el panel

    console.print(Panel(
        group,
        title="🎵 Syncatuna 🎵",
        subtitle="URL=agregar · n=next · p=pausa · r=reanudar · q=salir",
        subtitle_align="left",
        width=DASHBOARD_WIDTH,
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
            emit(f"{msg.get('message', 'Error del servidor.')}")


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
        # Cancelamos la carga anterior si todavía existe.
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

        # Si la pista todavía se está cargando, NO intentamos
        # hacer seek/pause sobre ella todavía.
        if state.loaded_track_id == new_id:
            await asyncio.to_thread(
                mpv.seek_abs,
                state.expected_position()
            )

            await asyncio.to_thread(
                mpv.set_pause,
                not state.playing
            )

    #nininini
    if track_changed or queue_changed or users_changed:
        announcements = None
        if added_ids:
            all_known = list(state.queue) + ([state.current] if state.current else [])
            added_tracks = [t for t in all_known if t["id"] in added_ids]
            announcements = [f"{t['added_by']} agregó: {t['title']}" for t in added_tracks]
        try:
            emit(render_dashboard(announcements=announcements, clear=bool(added_ids)))
        except Exception:
            pass


def fmt_time(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


def bottom_toolbar():
    if not state.current:
        return " (Nada sonando todavía — pega una URL de YouTube y Enter)"
    status = "▶" if state.playing else "⏸"
    pos = fmt_time(state.expected_position())
    dur = fmt_time(state.current["duration"])
    title = state.current["title"]
    if len(title) > 60:
        title = title[:57] + "..."
    return f" {status} {title}  |  {pos} / {dur}"


def print_help():
    emit(render_dashboard())


async def input_loop(ws):
    while True:
        try:
            line = await session.prompt_async("> ", bottom_toolbar=bottom_toolbar,
                                               refresh_interval=TICKER_INTERVAL)
        except (EOFError, KeyboardInterrupt):
            return  # Ctrl+D / Ctrl+C en el prompt: deja que main() limpie mpv
        line = line.strip()
        if not line:
            continue
        if line in ("q", "quit", "salir"):
            return  # deja que main() haga la limpieza (cerrar mpv, etc.)
        elif line in ("n", "next"):
            await ws.send(json.dumps({"type": "next"}))
        elif line in ("p", "pause", "pausa"):
            await ws.send(json.dumps({"type": "pause"}))
        elif line in ("r", "resume", "reanudar", "play"):
            await ws.send(json.dumps({"type": "resume"}))
        elif line in ("h", "help", "?"):
            print_help()
        elif line.startswith("http"):
            emit("… resolviendo con yt-dlp")
            meta = await asyncio.to_thread(fetch_metadata_local, line)
            if meta is not None:
                await ws.send(json.dumps({
                    "type": "add", "url": line,
                    "title": meta["title"], "duration": meta["duration"],
                }))
        else:
            print("Comando no reconocido. Escribe 'h' para ayuda.")


def install_shutdown_handlers(mpv: MPV):

    def _shutdown(signum, _frame):
        mpv.stop()
        os._exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _shutdown)


async def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print('Uso interno: client.py ws://IP_DEL_HOST:8765 "TuNombre"')
        return 2
    url, name = args[0], args[1]

    socket_path = os.path.join(tempfile.gettempdir(), f"syncatuna-mpv-{os.getpid()}.sock")
    mpv = MPV(socket_path)
    mpv.start()
    install_shutdown_handlers(mpv)

    try:
        with patch_stdout():
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"type": "hello", "name": name}))
                asyncio.create_task(clock_sync_loop(ws))
                recv_task = asyncio.create_task(receiver(ws, mpv))
                try:
                    await input_loop(ws)
                finally:
                    recv_task.cancel()
    finally:
        mpv.stop()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass