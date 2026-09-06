# Syncatuna

<p align="center">
  <img src="icon.svg" width="200">
</p>

**Syncatuna** is a CLI client/server application for synchronizing audio playback and managing collaborative playback queues across multiple machines using [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) and [`mpv`](https://mpv.io/).

The server acts as a shared synchronization layer and does not play or download audio. Each client uses its own local `yt-dlp` and `mpv` installation to resolve and play tracks.

> **Status:** Experimental. The project may still contain bugs and incomplete features.

## Installation

Syncatuna can be installed directly from the terminal:

```sh
curl -fsS https://raw.githubusercontent.com/GalletaOreo98/Syncatuna/refs/heads/main/installer.sh | sh
```

## Requirements

Syncatuna requires:

* Python **3.12 or newer**
* `curl` for installation

The Syncatuna **client** additionally requires:

* [`yt-dlp`](https://github.com/yt-dlp/yt-dlp)
* [`mpv`](https://mpv.io/)

`yt-dlp` and `mpv` are not installed automatically by Syncatuna. They must already be available in your `PATH`.

> The Syncatuna server does not require `yt-dlp` or `mpv`.

## Uninstallation

```sh
curl -fsS https://raw.githubusercontent.com/GalletaOreo98/Syncatuna/refs/heads/main/uninstaller.sh | sh
```

## Usage

### Start the server

Run the server on the default port:

```sh
syncatuna -s
```

Or specify a custom port:

```sh
syncatuna -s 8765
```

The server listens locally by default and is intended to be exposed through a reverse proxy such as Nginx when remote clients need to connect.

If you are using a VPN you can use `host = "0.0.0.0"` in your `config.toml` file
> server listening on 127.0.0.1:8765 (by default)

### Connect as a client

Examples:

```sh
syncatuna -c -n "Chris" localhost:8765
```

```sh
syncatuna -c -n "Chris" 100.107.203.3:8765
```

```sh
syncatuna -c -n "Chris" ws://localhost:8765
```

```sh
syncatuna -c -n "Chris" wss://syncatuna.example.com
```

## Configuration

Syncatuna stores its persistent configuration in:

```text
~/.config/syncatuna/config.toml
```

> CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP_NAME"


The configuration file is created automatically during the first installation.

Example (default values):

```toml
[server]
host = "127.0.0.1"
port = 8765

max_msg_size = 8192
watchdog_interval = 2.0

auto_advance_grace = 3.0
manual_advance_grace = 3.0

max_queue_size = 100
max_url_length = 2048
max_title_length = 200
max_name_length = 24
max_duration_seconds = 21600

add_cooldown_seconds = 2.0

[server.autofill]
enabled = true
max_client_tries = 2
response_timeout = 45.0

[client]
clock_sync_interval = 20.0
ticker_interval = 1.0

[client.autofill]
max_url_tries = 3
```

Configuration files are preserved when Syncatuna is updated, so user settings are not overwritten by new releases.

## Autofill

When the queue runs empty (a song ends or `next` is used with nothing else queued), the server can pick a random connected client to supply the next song automatically.

Each client that keeps a `favorites.txt` in its Syncatuna config directory is eligible to be chosen:

```text
~/.config/syncatuna/favorites.txt
```

> CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP_NAME"

The file holds one track per line, either as a plain URL or as `url - title`:

```text
https://youtu.be/dQw4w9WgXcQ - Rick Astley - Never Gonna Give You Up
https://www.youtube.com/watch?v=9bZkp7q19f0
https://youtu.be/hjJ-fEo0Vv8 - Some Artist - Song
```

Blank lines and lines starting with `#` are ignored, and entries are deduplicated by URL (the optional title just helps identify the track). A title that itself contains ` - ` is fine: the URL part never contains spaces.

### Adding a whole playlist

`syncatuna -f "PLAYLIST_URL"` (alias `-favorites`/`--favorites`) fetches the tracks of a YouTube playlist with `yt-dlp --flat-playlist`, appends what is new to your `favorites.txt` (never deleting what was already there) and re-sorts the file:

```sh
syncatuna -favorites "https://youtube.com/playlist?list=PLxxx..."
# [Syncatuna] Favorites: 12 added, 3 unavailable, 30 fetched - 42 total
```

Only URLs that are **available at the moment the command runs** are added: new tracks are resolved individually with `yt-dlp` (in parallel) so private, hidden, geo-blocked or deleted videos are skipped (reported as `unavailable`). Tracks already in the file are not re-checked and are never removed.

It is a standalone mode, mutually exclusive with `-s`/`-c`.

At connect time the client tells the server how many favorites it has (the server never sees the URLs themselves). When the queue runs out, the server:

1. picks a client at random from those with favorites,
2. asks it to pick one of its URLs and resolve it with its own `yt-dlp` and
3. queues the result.

If a track fails to resolve, the client tries other URLs from its list (up to `[client] autofill.max_url_tries`), and if all of them fail the server moves on to another client (up to `[server] autofill.max_client_tries`). Autofill can be disabled with `[server] autofill.enabled = false`.

## `yt-dlp` Configuration

Syncatuna intentionally keeps its own `yt-dlp` arguments to a minimum.

The client uses the `yt-dlp` executable available in the user's `PATH`, allowing users to configure `yt-dlp` themselves according to their environment.

For example, users may configure their own `yt-dlp` installation to use browser cookies when required:

```text
--cookies-from-browser firefox
```

> /home/YOUR_USER/.config/yt-dlp/config

This configuration belongs to `yt-dlp`, not to Syncatuna.

The same local `yt-dlp` installation is used by the client for metadata extraction and by `mpv` through its `ytdl_hook` integration.

## How It Works

Syncatuna uses a client/server architecture.

```text
                 Syncatuna Server
                ┌─────────────────┐
                │ Shared queue    │
                │ Master clock    │
                │ Playback state  │
                └────────┬────────┘
                         │
                    WebSocket
              ┌──────────┼──────────┐
              │          │          │
              ▼          ▼          ▼
           Client     Client     Client
              │          │          │
            yt-dlp     yt-dlp     yt-dlp
              │          │          │
             mpv        mpv        mpv
```

The server does not download or play media. Instead, it synchronizes which track should be playing and where playback should be according to a shared server-side timeline.

Each client resolves the media locally and controls its own `mpv` instance.
