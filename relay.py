#!/usr/bin/env python3
"""
Frame-aligned relay: pull the local HTTP stream and feed cliraop
with exact 1408-byte blocks (352 frames x 4 bytes), so PCM frames
never get misaligned by short reads.

Usage:
    python3 relay.py <server_ip> [--port 9000] [--target <homepod_ip>] [--out <file>]

If --out is given, write aligned PCM to a file instead of cliraop (for testing).
"""

import argparse
import http.client
import os
import subprocess
import sys

CHUNK = 1408  # 352 frames * 2ch * 2bytes
WAV_HEADER_SIZE = 44


def read_aligned(resp, proc=None, outfile=None, prebuffer_ms=2000):
    """Read the stream, skip WAV header, pre-buffer, then write CHUNK-aligned blocks.

    Pre-buffering lets cliraop receive a stable, continuous stream (like reading
    a file) instead of trickle-fed data, which avoids ALAC/RTP glitches.
    """
    # Skip WAV header
    wav_head = resp.read(WAV_HEADER_SIZE)
    if wav_head[:4] != b"RIFF":
        print(f"WARNING: no WAV header (got {wav_head[:4]}), resyncing", file=sys.stderr, flush=True)
        buf = wav_head
    else:
        print(f"WAV header OK: {wav_head[:12]}", file=sys.stderr, flush=True)
        buf = b""

    # Pre-buffer: accumulate enough audio before feeding cliraop
    prebuffer_bytes = (44100 * 4 * prebuffer_ms) // 1000
    print(f"Pre-buffering {prebuffer_bytes} bytes ({prebuffer_ms}ms)...", file=sys.stderr, flush=True)
    while len(buf) < prebuffer_bytes:
        data = resp.read(CHUNK)
        if not data:
            data = b"\x00" * CHUNK
        buf += data
    print("Pre-buffer ready, starting relay", file=sys.stderr, flush=True)

    # Drain pre-buffer into cliraop immediately (large aligned blocks),
    # so cliraop's very first reads never see a short pipe
    while len(buf) >= CHUNK:
        block = buf[:CHUNK]
        buf = buf[CHUNK:]
        if outfile:
            outfile.write(block)
        if proc:
            proc.stdin.write(block)
            proc.stdin.flush()

    bytes_written = 0
    # Write in large blocks so cliraop's read(1408) never sees a short pipe
    WRITE_BLOCK = 64 * 1024  # 64KB = ~46 chunks of 1408
    try:
        while True:
            data = resp.read(CHUNK)
            if not data:
                data = b"\x00" * CHUNK
            buf += data

            # Drain into large aligned blocks (multiple of CHUNK)
            while len(buf) >= WRITE_BLOCK:
                block = buf[:WRITE_BLOCK]
                buf = buf[WRITE_BLOCK:]
                if outfile:
                    outfile.write(block)
                if proc:
                    proc.stdin.write(block)
                    proc.stdin.flush()
                bytes_written += len(block)

            # Flush remaining full CHUNKs to avoid stalling
            while len(buf) >= CHUNK:
                block = buf[:CHUNK]
                buf = buf[CHUNK:]
                if outfile:
                    outfile.write(block)
                if proc:
                    proc.stdin.write(block)
                    proc.stdin.flush()
                bytes_written += len(block)
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        print("cliraop closed", file=sys.stderr, flush=True)
    finally:
        if proc:
            proc.stdin.close()
            proc.terminate()
        if outfile:
            outfile.close()
    print(f"Total bytes relayed: {bytes_written}", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("server_ip", help="local machine running audio_stream_server")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--target", default=os.environ.get("HOMEPOD_IP"),
                        help="HomePod IP to stream to via cliraop (default: $HOMEPOD_IP)")
    parser.add_argument("--out", help="write aligned PCM to this file instead of cliraop")
    args = parser.parse_args()
    if not args.target:
        parser.error("--target is required (or set HOMEPOD_IP, or set it in config.env)")

    # Connect to the HTTP stream
    conn = http.client.HTTPConnection(args.server_ip, args.port, timeout=10)
    conn.request("GET", "/stream")
    resp = conn.getresponse()
    print(f"HTTP {resp.status} {resp.reason}", file=sys.stderr, flush=True)

    outfile = None
    proc = None
    if args.out:
        outfile = open(args.out, "wb")
    else:
        # Use ALAC encoding (-a): HomePod stereo pair rejects PCM, accepts ALAC
        # Low volume (-v 20) so tests aren't deafening
        cliraop_bin = os.environ.get("CLIRAOP_BIN", "/tmp/cliraop")
        proc = subprocess.Popen(
            [cliraop_bin, "-a", "-v", "22", args.target, "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        # Log cliraop errors
        import threading

        def _drain_stderr():
            for line in iter(proc.stderr.readline, b""):
                print(f"[cliraop] {line.decode(errors='replace').rstrip()}", file=sys.stderr, flush=True)
            proc.stderr.close()

        threading.Thread(target=_drain_stderr, daemon=True).start()

    read_aligned(resp, proc, outfile)


if __name__ == "__main__":
    main()
