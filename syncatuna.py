#!/usr/bin/env python3
"""Syncatuna command-line launcher"""
from __future__ import annotations

import argparse
import asyncio
import sys

import client
import server

VERSION = "0.3.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="syncatuna",
        description="Synchronizes YouTube music between multiple clients in real time.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  syncatuna -s 8765\n"
            '  syncatuna -c -n "Chris" localhost:8765\n'
            '  syncatuna -c -n "Chris" 100.64.0.10:8765\n'
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("-s", "--server", nargs="?", const=8765, type=valid_port, metavar="PORT",
                      help="start the server (default port: 8765)")
    mode.add_argument("-c", "--client", action="store_true",
                      help="start the client")
    parser.add_argument("-n", "--name", metavar="NAME",
                        help="name visible in the room (required for the client)")
    parser.add_argument("endpoint", nargs="?", metavar="HOST:PORT",
                        help="server to connect to; may include ws:// or wss://")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def valid_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be a number") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def normalize_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint:
        raise ValueError("the server target cannot be empty")
    if endpoint.startswith(("ws://", "wss://")):
        return endpoint
    # Accepts HOST:PORT and HOST without a scheme
    return f"ws://{endpoint}"


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.client:
        if not args.name:
            parser.error("-c requires -n/--name")
        if not args.endpoint:
            parser.error("-c requires HOST:PORT")
        try:
            url = normalize_endpoint(args.endpoint)
        except ValueError as exc:
            parser.error(str(exc))
        if args.server is not None:
            parser.error("-s/--server and -c/--client cannot be used together")
        result = asyncio.run(client.main([url, args.name]))
        return int(result or 0)

    if args.name or args.endpoint:
        parser.error("-n/--name and HOST:PORT are only used with -c/--client")

    # --server uses nargs="?" and leaves the port in args.server
    result = asyncio.run(server.main([str(args.server)]))
    return int(result or 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
