# Syncatuna
<p align="center">
<img src="icon.svg" width="200">  
</p>

Cliente/servidor CLI para sincronizar reproducción de audio y crear colas de reproducción entre varias máquinas usando `yt-dlp` y `mpv`.

> Versioón de prueba, tiene errores básicos aún

## Instalación

El instalador comprueba primero que existan `yt-dlp` y `mpv`. Después comprueba `python3` y `curl`.
No instala `mpv` ni `yt-dlp` automáticamente: debes tenerlos disponibles en `PATH`.

```sh
curl -fsS https://raw.githubusercontent.com/GalletaOreo98/Syncatuna/refs/heads/main/installer.sh | sh
```

Después:

```sh
syncatuna -h
```

## Uso

Servidor:

```sh
syncatuna -s 8765
```

Cliente:

```sh
syncatuna -c -n "Chris" localhost:8765
```

También acepta un URI WebSocket completo:

```sh
syncatuna -c -n "Chris" ws://localhost:8765
```

