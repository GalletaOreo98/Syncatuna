#!/usr/bin/env python3
"""Syncatuna command-line launcher"""
from __future__ import annotations

import argparse
import asyncio
import sys

import client
import server

VERSION = "0.0.1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="syncatuna",
        description="Sincroniza música de YouTube entre varios clientes en tiempo real.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            "  syncatuna -s 8765\n"
            '  syncatuna -c -n "Chris" localhost:8765\n'
            '  syncatuna -c -n "Chris" 100.64.0.10:8765\n'
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("-s", "--server", nargs="?", const=8765, type=valid_port, metavar="PORT",
                      help="inicia el servidor (puerto por defecto: 8765)")
    mode.add_argument("-c", "--client", action="store_true",
                      help="inicia el cliente")
    parser.add_argument("-n", "--name", metavar="NAME",
                        help="nombre visible en la sala (requerido para el cliente)")
    parser.add_argument("endpoint", nargs="?", metavar="HOST:PORT",
                        help="servidor al que conectarse; puede incluir ws:// o wss://")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def valid_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("el puerto debe ser un número") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("el puerto debe estar entre 1 y 65535")
    return port


def normalize_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint:
        raise ValueError("el destino del servidor no puede estar vacío")
    if endpoint.startswith(("ws://", "wss://")):
        return endpoint
    # Acepta HOST:PORT y HOST sin esquema
    return f"ws://{endpoint}"


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.client:
        if not args.name:
            parser.error("-c requiere -n/--name")
        if not args.endpoint:
            parser.error("-c requiere HOST:PORT")
        try:
            url = normalize_endpoint(args.endpoint)
        except ValueError as exc:
            parser.error(str(exc))
        if args.server is not None:
            parser.error("-s/--server y -c/--client no se pueden usar a la vez")
        result = asyncio.run(client.main([url, args.name]))
        return int(result or 0)

    if args.name or args.endpoint:
        parser.error("-n/--name y HOST:PORT solo se usan con -c/--client")

    # --server usa nargs="?" y deja el puerto en args.server
    result = asyncio.run(server.main([str(args.server)]))
    return int(result or 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
