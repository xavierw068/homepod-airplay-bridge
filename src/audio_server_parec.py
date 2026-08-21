#!/usr/bin/env python3
"""
HTTP audio stream server using parec (PulseAudio monitor) instead of GStreamer.

GStreamer's pipewiresrc fails to preroll on the loopback monitor, but
parec captures it reliably. Outputs WAV-header + PCM over HTTP.
"""

import argparse
import http.server
import os
import subprocess
import struct
import sys
import threading

SAMPLE_RATE = 44100
CHANNELS = 2
FORMAT = "s16le"

_running = True


class AudioHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    sink_name = "homepod-bridge-sink"

    def do_GET(self):
        print(f"HTTP client: {self.client_address}", file=sys.stderr, flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", "999999999")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        # WAV header
        wav_header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36, b"WAVE",
            b"fmt ", 16, 1, CHANNELS, SAMPLE_RATE,
            SAMPLE_RATE * CHANNELS * 2, CHANNELS * 2, 16,
            b"data", 0xFFFFFFFF,
        )
        try:
            self.wfile.write(wav_header)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        # parec: capture from the bridge sink monitor
        proc = subprocess.Popen(
            [
                "parec",
                f"--device={self.sink_name}.monitor",
                f"--format={FORMAT}",
                f"--rate={SAMPLE_RATE}",
                f"--channels={CHANNELS}",
            ],
            stdout=subprocess.PIPE,
        )
        print("parec started", file=sys.stderr, flush=True)
        try:
            while _running:
                data = proc.stdout.read(4096)
                if not data:
                    break
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            print("client disconnected", file=sys.stderr, flush=True)
        finally:
            proc.terminate()

    def log_message(self, format, *args):
        pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="Serve PipeWire audio via HTTP (parec-based)")
    parser.add_argument("--http-port", type=int, default=9000)
    parser.add_argument("--sink", default=os.environ.get("BRIDGE_SINK", "homepod-bridge-sink"))
    args = parser.parse_args()

    AudioHandler.sink_name = args.sink
    server = Server(("0.0.0.0", args.http_port), AudioHandler)
    print(f"HTTP audio server (parec) listening on :{args.http_port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _running = False
        server.shutdown()


if __name__ == "__main__":
    main()
