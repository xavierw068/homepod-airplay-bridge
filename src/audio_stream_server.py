#!/usr/bin/env python3
"""
HTTP audio stream server for HomePod Bridge.

Captures audio from the PipeWire virtual sink (homepod-bridge-sink)
and serves it via HTTP for squeeze2raop to fetch and play to HomePods.

Usage:
    python3 audio_stream_server.py --http-port 9000
"""

import array
import argparse
import os
import queue
import socket
import threading
import time
import http.server
import sys
from urllib.parse import urlparse

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import GLib, Gst, GstApp

# Global audio buffer (thread-safe)
audio_queue = queue.Queue(maxsize=100)  # ~100 buffers of 352 samples each
_running = True

# Broadcast infrastructure: every HTTP client gets its OWN queue, and a
# single dispatcher thread copies each captured buffer into every client's
# queue. Without this, multiple clients (the two squeeze2raop players) read
# from the SAME queue and split the data rate - with two players each got
# ~1/3 of real-time, so the stream's playhead fell ~8s behind wall-clock
# and the HomePods never got enough audio to play. Each client's queue drops
# the oldest block when full, so a slow client never blocks the others.
_client_queues = set()
_client_lock = threading.Lock()
SILENCE_BLOCK = b"\x00" * 1408          # 352 frames * 2ch * 2bytes = 8ms
SILENCE_RATE = 44100 * 2 * 2            # bytes/sec for S16LE stereo 44100


def _dupe_channel(data: bytes, channel: str) -> bytes:
    """Duplicate one channel of interleaved S16LE stereo into both channels.

    'L' keeps the even-indexed samples, 'R' the odd-indexed ones. Output is
    the same length as the input (1408 -> 1408), so the 352-frame alignment
    invariant is preserved and each HomePod independently mono-downmixes its
    stream to just that one channel's content (true L/R separation). Blocks
    that are not a whole number of stereo frames are passed through unchanged
    so alignment can never slip. Returns a NEW bytes object - the shared block
    broadcast by _broadcast() is never mutated.
    """
    if channel not in ("L", "R") or len(data) % 4 != 0:
        return data
    a = array.array("h")          # native endian = S16LE on this x86 box
    a.frombytes(data)
    ch = a[0::2] if channel == "L" else a[1::2]   # 352 int16
    out = array.array("h", [0]) * (len(ch) * 2)   # 704 int16
    out[0::2] = ch
    out[1::2] = ch
    return out.tobytes()


def _broadcast(data: bytes):
    """Copy one audio block into every connected client's queue."""
    with _client_lock:
        for q in list(_client_queues):
            try:
                q.put(data, block=False)
            except queue.Full:
                # drop oldest so the client never falls further behind
                try:
                    q.get_nowait()
                    q.put(data, block=False)
                except (queue.Empty, queue.Full):
                    pass


def _dispatcher():
    """Single reader of the capture queue; feeds silence when idle.

    Runs in a background thread. Reads a real audio block (or paces silence
    at the real-time rate) and broadcasts it to all clients, so every client
    receives the full-rate stream. While idle, sleeps until the next silence
    block is due (~125 wakeups/s) instead of busy-polling every 2 ms.
    """
    block_secs = len(SILENCE_BLOCK) / SILENCE_RATE  # ~7.98 ms per block
    silence_mark = None
    while _running:
        try:
            data = audio_queue.get(timeout=0.5)
            _broadcast(data)
        except queue.Empty:
            now = time.monotonic()
            if silence_mark is None:
                silence_mark = now
            due = int((now - silence_mark) * SILENCE_RATE / len(SILENCE_BLOCK))
            if due >= 1:
                _broadcast(SILENCE_BLOCK * due)
                silence_mark += len(SILENCE_BLOCK) * due / SILENCE_RATE
                now = time.monotonic()
            # Sleep only until the next silence block is due. When fully
            # caught up this is ~8 ms -> ~125 wakeups/s instead of 500.
            delay = (silence_mark + block_secs) - now
            if delay > 0:
                time.sleep(min(delay, 0.5))


class AudioCapture:
    """GStreamer capture from PipeWire sink, feeding HTTP server."""

    SAMPLE_RATE = 44100
    CHANNELS = 2
    FORMAT = "S16LE"

    def __init__(self, sink_name="homepod-bridge-sink"):
        Gst.init(None)
        self.sink_name = sink_name
        self.pipeline = None
        self.appsink = None
        self.loop = None

    def _build_pipeline(self):
        pipeline = Gst.Pipeline.new("homepod-audio-server")

        # Use pulsesrc (PulseAudio/PipeWire compat) to reliably capture the
        # sink's monitor ports. pipewiresrc's target-object matching was
        # grabbing the mic input instead of the sink monitor.
        src = Gst.ElementFactory.make("pulsesrc", "pw-src")
        src.set_property("device", f"{self.sink_name}.monitor")

        convert = Gst.ElementFactory.make("audioconvert", "convert")
        resample = Gst.ElementFactory.make("audioresample", "resample")

        caps = Gst.Caps.from_string(
            f"audio/x-raw,format={self.FORMAT},"
            f"rate={self.SAMPLE_RATE},"
            f"channels={self.CHANNELS},"
            f"layout=interleaved"
        )
        capsfilter = Gst.ElementFactory.make("capsfilter", "caps")
        capsfilter.set_property("caps", caps)

        appsink = Gst.ElementFactory.make("appsink", "app-sink")
        appsink.set_property("emit-signals", True)
        appsink.set_property("max-buffers", 20)
        appsink.set_property("drop", True)  # Drop old buffers if we're slow
        appsink.set_property("sync", False)

        appsink.connect("new-sample", self._on_new_sample)

        for elem in [src, convert, resample, capsfilter, appsink]:
            pipeline.add(elem)

        src.link(convert)
        convert.link(resample)
        resample.link(capsfilter)
        capsfilter.link(appsink)

        self.appsink = appsink
        return pipeline

    def _on_new_sample(self, appsink):
        sample = appsink.pull_sample()
        if sample is None:
            return Gst.FlowReturn.ERROR

        buf = sample.get_buffer()
        result, map_info = buf.map(Gst.MapFlags.READ)
        if result:
            pcm_data = bytes(map_info.data)
            buf.unmap(map_info)

            # Put audio data in the shared queue (non-blocking)
            try:
                audio_queue.put(pcm_data, block=False)
            except queue.Full:
                # Drop oldest if full (real-time streaming)
                try:
                    audio_queue.get_nowait()
                    audio_queue.put(pcm_data, block=False)
                except queue.Empty:
                    pass

        return Gst.FlowReturn.OK

    def start(self):
        self.pipeline = self._build_pipeline()

        # Bus watcher: log errors/warnings
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        def _on_bus_msg(bus, msg):
            t = msg.type
            if t == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                print(f"Pipeline ERROR: {err.message} ({dbg})", file=sys.stderr)
            elif t == Gst.MessageType.WARNING:
                err, dbg = msg.parse_warning()
                print(f"Pipeline WARNING: {err.message}", file=sys.stderr)
        bus.connect("message", _on_bus_msg)

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print("Failed to start audio capture pipeline", file=sys.stderr)
            return False

        # Run GStreamer main loop in a background thread (required for async
        # state changes and data flow with pulsesrc).
        self.loop = GLib.MainLoop()
        import threading
        self._glib_thread = threading.Thread(target=self.loop.run, daemon=True)
        self._glib_thread.start()

        print(f"Audio capture started: {self.FORMAT}/{self.SAMPLE_RATE}Hz/{self.CHANNELS}ch",
              file=sys.stderr)
        return True

    def stop(self):
        if self.loop:
            self.loop.quit()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)


class AudioHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Serve raw PCM audio over HTTP."""

    protocol_version = "HTTP/1.0"

    def do_GET(self):
        # Route by path: /stream/L serves only the left channel, /stream/R
        # only the right (each duplicated into a stereo frame so alignment,
        # WAV header and ALAC encoding stay untouched). Any other path -
        # including /stream (the relay scripts) - serves full stereo.
        path = urlparse(self.path).path
        channel = "L" if path == "/stream/L" else ("R" if path == "/stream/R" else None)
        print(f"HTTP client connected: {self.client_address} channel={channel or 'stereo'}")
        self.send_response(200)
        # WAV format - user wants to keep audio intact for later EQ/DSP tuning
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", "999999999")  # Infinite
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        # WAV header so the player can parse the format
        import struct as _struct
        wav_header = _struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36, b"WAVE",
            b"fmt ", 16, 1, 2, 44100, 44100 * 2 * 2, 2 * 2, 16,
            b"data", 0xFFFFFFFF
        )
        try:
            self.wfile.write(wav_header)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        # Each client gets its own queue, fed at full rate by the dispatcher
        # thread (see _dispatcher). A client's queue dropping the oldest block
        # on overflow keeps a slow reader from stalling the others.
        my_queue = queue.Queue(maxsize=100)
        with _client_lock:
            _client_queues.add(my_queue)

        try:
            while _running:
                try:
                    data = my_queue.get(timeout=0.5)
                    if channel is not None:
                        data = _dupe_channel(data, channel)
                    self.wfile.write(data)
                    self.wfile.flush()
                except queue.Empty:
                    time.sleep(0.005)  # stay responsive; dispatcher paces data
        except (BrokenPipeError, ConnectionResetError):
            print(f"HTTP client disconnected: {self.client_address}")
        except Exception as e:
            print(f"HTTP stream error: {e}")
        finally:
            with _client_lock:
                _client_queues.discard(my_queue)

    def log_message(self, format, *args):
        pass  # Suppress default logging


class AudioHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="Serve PipeWire audio via HTTP")
    parser.add_argument("--http-port", type=int, default=9000)
    parser.add_argument("--sink", default=os.environ.get("BRIDGE_SINK", "homepod-bridge-sink"))
    args = parser.parse_args()

    global _running

    # Start audio capture
    capture = AudioCapture(sink_name=args.sink)
    if not capture.start():
        sys.exit(1)

    # Dispatcher thread: reads the capture queue once and broadcasts to every
    # connected client, so each player gets the full-rate stream.
    threading.Thread(target=_dispatcher, daemon=True).start()

    # Start HTTP server
    server = AudioHTTPServer(("0.0.0.0", args.http_port), AudioHTTPHandler)
    print(f"HTTP audio server listening on port {args.http_port}", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _running = False
        capture.stop()
        server.shutdown()
        print("\nStopped", file=sys.stderr)


if __name__ == "__main__":
    main()
