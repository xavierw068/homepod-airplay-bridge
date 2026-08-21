#!/usr/bin/env python3
"""
Relay: capture homepod-bridge-sink.monitor via parec, feed cliraop (ALAC).

Bypasses HTTP/GStreamer entirely. Requires DACP broadcast running.
Usage:
    python3 relay_parec.py [target_ip]
"""

import os
import subprocess
import sys
import time

CHUNK = 1408  # 352 frames * 2ch * 2bytes
TARGET = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HOMEPOD_IP")
CLIRAOP_BIN = os.environ.get("CLIRAOP_BIN", "/tmp/cliraop")
PREBUFFER_MS = 2000
PREBUFFER_BYTES = (44100 * 4 * PREBUFFER_MS) // 1000


def drain(proc, prebuffer=True):
    """Read from parec and feed cliraop in aligned blocks."""
    buf = b""
    written = 0

    # Start cliraop
    cliraop = subprocess.Popen(
        [CLIRAOP_BIN, "-a", "-v", "90", TARGET, "-"],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Drain cliraop stderr
    import threading

    def _err():
        for line in iter(cliraop.stderr.readline, b""):
            print(f"[cliraop] {line.decode(errors='replace').rstrip()}", file=sys.stderr, flush=True)
        cliraop.stderr.close()

    threading.Thread(target=_err, daemon=True).start()

    # Pre-buffer
    print(f"Pre-buffering {PREBUFFER_MS}ms...", file=sys.stderr, flush=True)
    while len(buf) < PREBUFFER_BYTES:
        data = proc.stdout.read(CHUNK)
        if not data:
            data = b"\x00" * CHUNK
        buf += data
    print("Pre-buffer ready", file=sys.stderr, flush=True)

    # Drain pre-buffer into cliraop first
    while len(buf) >= CHUNK:
        block = buf[:CHUNK]
        buf = buf[CHUNK:]
        cliraop.stdin.write(block)
        cliraop.stdin.flush()
        written += len(block)

    try:
        while True:
            data = proc.stdout.read(CHUNK)
            if not data:
                data = b"\x00" * CHUNK
            buf += data
            while len(buf) >= CHUNK:
                block = buf[:CHUNK]
                buf = buf[CHUNK:]
                cliraop.stdin.write(block)
                cliraop.stdin.flush()
                written += len(block)
    except BrokenPipeError:
        print("cliraop closed", file=sys.stderr, flush=True)
    finally:
        cliraop.terminate()


def main():
    if not TARGET:
        print("TARGET (HOMEPOD_IP) is required: set it in config.env or pass as argv[1]",
              file=sys.stderr, flush=True)
        sys.exit(1)
    print(f"Starting parec relay -> {TARGET}", file=sys.stderr, flush=True)
    while True:
        proc = subprocess.Popen(
            [
                "parec",
                "--device=homepod-bridge-sink.monitor",
                "--format=s16le",
                "--rate=44100",
                "--channels=2",
            ],
            stdout=subprocess.PIPE,
        )
        print("parec started", file=sys.stderr, flush=True)
        try:
            drain(proc)
        except Exception as e:
            print(f"relay error: {e}", file=sys.stderr, flush=True)
        proc.terminate()
        print("restarting relay in 2s", file=sys.stderr, flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
