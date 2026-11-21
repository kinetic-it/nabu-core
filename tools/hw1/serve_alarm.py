#!/usr/bin/env python3
"""H-W1: serve the ring file and prove who fetched it.

The Echo fetches the media URL itself, so a plain file server is enough — and its access
log is the only hard evidence that the device really pulled the file (instead of HA
answering with an error that still looks like "accepted").

    python3 tools/hw1/serve_alarm.py --file assets/hw1/bell.wav --port 8085 --seconds 900

Every request is appended to the JSONL log: time, client address, path, bytes sent,
range header. `--seconds` bounds the run; the server exits by itself.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import threading
import time

LOG_PATH = ""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:                      # noqa: N802 (http.server API)
        if self.path.split("?")[0] != self.server.allowed_path:
            self.send_error(404)
            self._log(0, "not_found")
            return
        try:
            with open(self.server.file_path, "rb") as handle:
                payload = handle.read()
        except OSError:
            self.send_error(500)
            self._log(0, "read_error")
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Accept-Ranges", "none")
        self.end_headers()
        try:
            self.wfile.write(payload)
            sent = len(payload)
        except (BrokenPipeError, ConnectionResetError):
            sent = 0                                # the device hung up: still worth recording
        self._log(sent, "ok")

    def do_HEAD(self) -> None:                     # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(os.path.getsize(self.server.file_path)))
        self.end_headers()
        self._log(0, "head")

    def _log(self, sent: int, verdict: str) -> None:
        row = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "client": self.client_address[0],
            "path": self.path,
            "bytes": sent,
            "verdict": verdict,
            "range": self.headers.get("Range"),
        }
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"FETCH {row['client']} {row['path']} {sent}B {verdict}", flush=True)

    def log_message(self, fmt: str, *args) -> None:  # keep the console for our own lines
        return


def main() -> None:
    global LOG_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--port", type=int, default=8085)
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--log", default="hw1-fetch.jsonl")
    args = parser.parse_args()

    if not os.path.isfile(args.file):
        raise SystemExit(f"no such file: {args.file}")
    LOG_PATH = args.log
    allowed = "/" + os.path.basename(args.file)

    with socketserver.ThreadingTCPServer(("0.0.0.0", args.port), Handler) as server:
        server.daemon_threads = True
        server.allow_reuse_address = True
        server.file_path = args.file
        server.allowed_path = allowed
        print(f"serving {args.file} at http://<host-ip>:{args.port}{allowed} for {args.seconds}s", flush=True)
        timer = threading.Timer(args.seconds, server.shutdown)
        timer.daemon = True
        timer.start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            timer.cancel()
    print("server stopped", flush=True)


if __name__ == "__main__":
    main()
