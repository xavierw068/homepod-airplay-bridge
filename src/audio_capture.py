#!/usr/bin/env python3
"""
Audio capture from PipeWire virtual sink via GStreamer.

NOTE: this is a LEGACY / experimental capture path (uses pipewiresrc,
which grabs the wrong PipeWire node on some setups). The working capture
is src/audio_stream_server.py (pulsesrc + per-client broadcast queues).
Kept here for UNIX-socket / WAV-file experiments.

Captures audio from homepod-bridge-sink monitor ports,
resamples to S16LE/44100Hz/stereo (AirPlay v2 standard),
and outputs to a UNIX socket (for squeeze2raop) or WAV file (for testing).

Usage:
    # Test mode: capture 5 seconds to WAV file
    python3 audio_capture.py --test --duration 5 --output test.wav

    # Socket mode: stream to squeeze2raop via UNIX socket
    python3 audio_capture.py --socket /tmp/homepod-stream.sock
"""

import argparse
import signal
import struct
import sys
import wave

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import GLib, Gst, GstApp


class AudioCapture:
    """Captures audio from PipeWire monitor and outputs PCM S16LE/44100Hz/stereo."""

    SAMPLE_RATE = 44100
    CHANNELS = 2
    FORMAT = "S16LE"
    # AirPlay uses 352 samples per frame
    FRAMES_PER_BUFFER = 352

    def __init__(self, sink_name: str = "homepod-bridge-sink"):
        Gst.init(None)
        self.sink_name = sink_name
        self.pipeline = None
        self.appsink = None
        self.loop = GLib.MainLoop()
        self.running = True
        self.bytes_captured = 0

        # Output handlers
        self.wav_writer = None
        self.socket_fd = None

    def _build_pipeline(self) -> Gst.Pipeline:
        """Build GStreamer pipeline: pipewiresrc → audioconvert → audioresample → appsink."""
        pipeline = Gst.Pipeline.new("homepod-capture")

        # Source: PipeWire monitor ports
        src = Gst.ElementFactory.make("pipewiresrc", "pw-src")
        src.set_property("target-object", self.sink_name)

        # Audio format conversion
        convert = Gst.ElementFactory.make("audioconvert", "convert")
        resample = Gst.ElementFactory.make("audioresample", "resample")

        # Caps filter: force S16LE/44100Hz/stereo
        caps = Gst.Caps.from_string(
            f"audio/x-raw,format={self.FORMAT},"
            f"rate={self.SAMPLE_RATE},"
            f"channels={self.CHANNELS},"
            f"layout=interleaved"
        )
        capsfilter = Gst.ElementFactory.make("capsfilter", "caps")
        capsfilter.set_property("caps", caps)

        # Sink: appsink for pulling buffers
        appsink = Gst.ElementFactory.make("appsink", "app-sink")
        appsink.set_property("emit-signals", True)
        appsink.set_property("max-buffers", 20)
        appsink.set_property("drop", False)
        appsink.set_property("sync", False)

        # Connect new-sample signal
        appsink.connect("new-sample", self._on_new_sample)
        appsink.connect("eos", self._on_eos)

        # Add elements
        for elem in [src, convert, resample, capsfilter, appsink]:
            pipeline.add(elem)

        # Link
        src.link(convert)
        convert.link(resample)
        resample.link(capsfilter)
        capsfilter.link(appsink)

        self.appsink = appsink
        return pipeline

    def _on_new_sample(self, appsink: GstApp.AppSink) -> Gst.FlowReturn:
        """Called when a new audio buffer is available."""
        sample = appsink.pull_sample()
        if sample is None:
            return Gst.FlowReturn.ERROR

        buf = sample.get_buffer()
        result, map_info = buf.map(Gst.MapFlags.READ)
        if not result:
            return Gst.FlowReturn.ERROR

        pcm_data = bytes(map_info.data)
        buf.unmap(map_info)

        self.bytes_captured += len(pcm_data)

        # Output to WAV (test mode)
        if self.wav_writer:
            self.wav_writer.writeframesraw(pcm_data)

        # Output to UNIX socket (production mode)
        if self.socket_fd:
            try:
                self.socket_fd.sendall(pcm_data)
            except (BrokenPipeError, ConnectionResetError):
                print("Socket disconnected, stopping capture", file=sys.stderr)
                self.running = False
                self.loop.quit()
                return Gst.FlowReturn.EOS

        return Gst.FlowReturn.OK

    def _on_eos(self, appsink: GstApp.AppSink):
        """Called when end-of-stream is reached."""
        print("End of stream", file=sys.stderr)
        self.running = False
        self.loop.quit()

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message):
        """Handle pipeline bus messages."""
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"Pipeline error: {err.message}", file=sys.stderr)
            print(f"Debug: {debug}", file=sys.stderr)
            self.running = False
            self.loop.quit()
        elif t == Gst.MessageType.EOS:
            print("Pipeline EOS", file=sys.stderr)
            self.running = False
            self.loop.quit()
        elif t == Gst.MessageType.WARNING:
            err, debug = message.parse_warning()
            print(f"Warning: {err.message}", file=sys.stderr)

    def start(self):
        """Start the capture pipeline."""
        self.pipeline = self._build_pipeline()

        # Connect bus
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        # Start pipeline
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print("Failed to start pipeline", file=sys.stderr)
            return False

        print(f"Capture started: {self.FORMAT}/{self.SAMPLE_RATE}Hz/{self.CHANNELS}ch",
              file=sys.stderr)
        print(f"Source: {self.sink_name} (monitor ports)", file=sys.stderr)

        # Handle signals
        def signal_handler(sig, frame):
            print("\nStopping capture...", file=sys.stderr)
            self.stop()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Run main loop
        self.loop.run()
        return True

    def stop(self):
        """Stop the capture pipeline."""
        self.running = False
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        if self.wav_writer:
            self.wav_writer.close()
            self.wav_writer = None
        if self.socket_fd:
            self.socket_fd.close()
            self.socket_fd = None
        self.loop.quit()

    def open_wav(self, path: str):
        """Open a WAV file for test output."""
        self.wav_writer = wave.open(path, "wb")
        self.wav_writer.setnchannels(self.CHANNELS)
        self.wav_writer.setsampwidth(2)  # S16LE = 2 bytes
        self.wav_writer.setframerate(self.SAMPLE_RATE)
        print(f"WAV output: {path}", file=sys.stderr)

    def open_socket(self, path: str):
        """Open a UNIX socket for streaming output."""
        import socket
        self.socket_fd = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket_fd.connect(path)
        print(f"Socket output: {path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Capture audio from PipeWire for HomePod streaming")
    parser.add_argument("--sink", default="homepod-bridge-sink",
                        help="PipeWire sink name to capture from")
    parser.add_argument("--test", action="store_true",
                        help="Test mode: capture to WAV file")
    parser.add_argument("--duration", type=float, default=5.0,
                        help="Test mode: capture duration in seconds")
    parser.add_argument("--output", default="capture.wav",
                        help="Test mode: output WAV file path")
    parser.add_argument("--socket",
                        help="Socket mode: UNIX socket path for squeeze2raop")
    args = parser.parse_args()

    capture = AudioCapture(sink_name=args.sink)

    if args.test:
        capture.open_wav(args.output)
        # Schedule stop after duration
        GLib.timeout_add(int(args.duration * 1000), capture.stop)
    elif args.socket:
        capture.open_socket(args.socket)
    else:
        # Default: capture to stdout as raw PCM
        # Wrap stdout in binary mode
        import os
        capture.socket_fd = os.fdopen(sys.stdout.fileno(), "wb", closefd=False)
        print("PCM output: stdout (raw S16LE/44100Hz/stereo)", file=sys.stderr)

    capture.start()

    if args.test:
        duration_s = capture.bytes_captured / (44100 * 2 * 2)
        print(f"Captured {capture.bytes_captured} bytes ({duration_s:.1f}s)",
              file=sys.stderr)


if __name__ == "__main__":
    main()
