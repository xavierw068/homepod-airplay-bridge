#!/usr/bin/env python3
"""
Minimal slimproto server that feeds real-time audio to squeeze2raop.

This replaces LMS (Logitech Media Server) for the purpose of streaming
PipeWire audio to HomePods via squeeze2raop (RAOP/AirPlay bridge).

Protocol: https://wiki.slimdevices.com/index.php/SlimProto
The player (squeeze2raop) connects via TCP, sends HELO, then the server
sends a "strm" command pointing to an HTTP URL. The player fetches the
audio stream from that URL and plays it to the AirPlay device.

Usage:
    python3 slimproto_server.py --port 3483 --http-port 9000
"""

import argparse
import logging
import os
import socket
import struct
import threading
import time

# slimproto message formats
# All multi-byte values are big-endian

# RAOP volume target sent to the HomePod pair, in percent (pack_audg arg).
# Verified against a WORKING LMS session on the NAS: it drives the pair at
# ~50% (index 63 ~ -11.7 dB) and both devices stay audible. Sending 0 dB
# (=100%) instead was observed to make the pair coordinator push the
# secondary HomePod to volume 0 (muting the pair). Keep both channels at a
# moderate level so the coordinator never sees a "full" primary.
TARGET_VOLUME_PERCENT = 50

# How long the L/R pair's first-connected player waits for its partner before
# starting alone (sync-start). The two squeeze2raop players connect with a
# ~15s gap; starting them together avoids the playout-latency difference that
# produces an audible L/R desync. 30s covers the typical gap; if the partner
# never shows up, the first player starts on its own after this timeout.
STRM_SYNC_TIMEOUT = 30

def frame_packet(body: bytes) -> bytes:
    """Add the 2-byte length prefix used by slimproto."""
    return struct.pack(">H", len(body)) + body

def _normalize_mac(mac: str) -> str:
    """Normalize a colon-hex MAC to 12 lowercase hex chars for comparison."""
    return (mac or "").strip().replace(":", "").lower()

def pack_setd_name(name="HomePod Bridge"):
    """SETD packet to set device name."""
    # Format: "setd" + length(1 byte) + name
    body = b"setd" + bytes([len(name)]) + name.encode()
    return frame_packet(body)

def pack_audg(left=100, right=100):
    """AUDG packet to set gain (volume percent 0-100 per channel).

    squeezelite's audg_packet struct (22 bytes, all big-endian):
        opcode(4) old_gainL(4) old_gainR(4) adjust(1) preamp(1) gainL(4) gainR(4)

    NOTE: squeeze2raop's process_audg() derives the volume from old_gainL only
    (it has a bug: gain = (old_gainL + old_gainL) / 2, ignoring gainL/gainR),
    so the requested level MUST go into old_gainL/old_gainR. old_gain is an
    index 0..128 into LMSVolumeMap[] giving a 0..100 percent volume, which is
    then mapped to a dB value and sent to the RAOP device. The mapping is a
    near-linear approximation (index ~ percent * 1.28): 50% -> index 64
    => 61% => ~ -12 dB.

    We send TARGET_VOLUME_PERCENT (default 50 = index 64 = ~ -12 dB) as our
    target volume, matching the level a working LMS session drives the pair at.
    Loudness is fine-tuned via the PipeWire sink
    (`pactl set-sink-volume homepod-bridge-sink <pct>`). Sending 0 dB (=100%)
    instead was observed to make the pair coordinator push the secondary
    HomePod to volume 0, muting the pair; a moderate non-muted level avoids
    that.
    """
    index = min(128, round(left * 128 / 100))
    # opcode(4) + old_gainL(4) + old_gainR(4) + adjust(1) + preamp(1) + gainL(4) + gainR(4)
    body = (b"audg"
            + struct.pack(">II", index, index)  # old_gainL, old_gainR (volume)
            + bytes([1, 0])                     # adjust=1 (apply gain), preamp=0
            + struct.pack(">II", 0, 0))         # gainL, gainR (ignored by squeezelite)
    return frame_packet(body)

def pack_aude(enable=True):
    """AUDE packet to enable/disable audio output."""
    # Format: "aude" + enable_spdif + enable_dac
    body = b"aude" + bytes([1 if enable else 0, 1 if enable else 0])
    return frame_packet(body)

def pack_strm_start(server_ip, server_port, request_path, sample_rate=44100):
    """
    Create a "strm" start command.

    The player will connect to server_ip:server_port and send
    the request_path as an HTTP GET request.

    autostart=1 matches LMS (verified in the NAS raopbridge log: "strm s
    autostart: 1"). The player starts streaming as soon as it has buffered;
    the two HomePod sessions need not start at the same wall-clock moment -
    the RAOP NTP sync aligns them during playback (the NAS's own sessions
    start ~0.8s apart and the pair plays them fine). autostart=0 plus a 'u'
    with a far-future start time was tried and left the HomePods silent
    (the long coordinated wait appears to make the device time out the
    session).
    """
    opcode = b"strm"
    command = b"s"  # start
    autostart = b"1"  # start immediately after buffering (matches LMS)
    format_code = b"p"  # PCM
    pcm_sample_size = b"1"  # 16-bit (index 1 = 16)
    # Sample rate index: 0=11025, 1=22050, 2=32000, 3=44100, 4=48000
    rate_index = b"3"  # 44100
    pcm_channels = b"2"  # stereo
    pcm_endianness = b"0"  # little-endian
    threshold = b"\x00"
    spdif_enable = b"\x00"
    transition_period = b"\x00"
    transition_type = b"0"
    flags = b"\x00"
    output_threshold = b"\x00"
    slaves = b"\x00"
    replay_gain = struct.pack(">I", 0)
    server_port_b = struct.pack(">H", server_port)
    server_ip_b = socket.inet_aton(server_ip)

    # The request string (HTTP GET path)
    request = f"GET {request_path} HTTP/1.0\r\n\r\n".encode()

    body = opcode + command + autostart + format_code + pcm_sample_size + \
           rate_index + pcm_channels + pcm_endianness + threshold + \
           spdif_enable + transition_period + transition_type + flags + \
           output_threshold + slaves + replay_gain + server_port_b + server_ip_b + request

    # Wrap with 2-byte length prefix
    return frame_packet(body)


class SlimprotoServer:
    """Minimal slimproto server that streams audio to a squeeze2raop player."""

    def __init__(self, listen_ip="0.0.0.0", port=3483, http_port=9000,
                 device_name="HomePod Bridge", left_mac="", right_mac=""):
        self.listen_ip = listen_ip
        self.port = port
        self.http_port = http_port
        self.device_name = device_name
        self.players = []
        self.running = True
        self.audio_url = None

        # Optional L/R channel split: when a player's HELO MAC matches one of
        # these (from config.env, mirroring the <mac> in raopbridge.xml), that
        # player is sent /stream/L or /stream/R so each HomePod plays only its
        # own channel. Empty means that player gets the full stereo stream.
        self.left_mac = _normalize_mac(left_mac)
        self.right_mac = _normalize_mac(right_mac)
        if self.left_mac and self.right_mac and self.left_mac == self.right_mac:
            print("WARNING: HOMEPOD_LEFT_MAC == HOMEPOD_RIGHT_MAC; both "
                  "matching players will be treated as LEFT")
        elif bool(self.left_mac) != bool(self.right_mac):
            print("WARNING: only one of HOMEPOD_LEFT_MAC/HOMEPOD_RIGHT_MAC is "
                  "set; the unmatched player will get full stereo")
        if self.left_mac or self.right_mac:
            print(f"L/R split configured: left={self.left_mac or '(unset)'} "
                  f"right={self.right_mac or '(unset)'}")

        # Sync-start coordination: in L/R mode, hold each player's strm until
        # BOTH have connected, so the two RAOP sessions begin together (avoids
        # the playout-latency gap that makes the pair sound desynced). Guarded
        # by _strm_cond; _strm_pending maps mac -> {conn, suffix}; _strm_solo
        # turns on once a timeout lets the first player start alone, so a late
        # second player starts immediately instead of waiting again.
        self._strm_pending = {}
        self._strm_cond = threading.Condition()
        self._strm_solo = False

    def start(self):
        """Start the slimproto server."""
        # UDP discovery listener (respond to player's discovery broadcast)
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.udp_sock.bind((self.listen_ip, self.port))
        self.udp_sock.settimeout(1.0)
        udp_thread = threading.Thread(target=self._discovery_listener, daemon=True)
        udp_thread.start()
        print(f"UDP discovery listening on {self.listen_ip}:{self.port}")

        # TCP slimproto server
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.listen_ip, self.port))
        self.sock.listen(5)
        print(f"slimproto server listening on {self.listen_ip}:{self.port}")

        # CLI server (port 9090): squeezelite players open a CLI connection
        # when reconnecting. Without it, a broken player connection loops
        # forever trying to reconnect ("unable to connect to server with cli").
        cli_thread = threading.Thread(target=self._cli_server, daemon=True)
        cli_thread.start()

        # Keepalive thread: send a packet every 4s to keep connections alive
        keepalive_thread = threading.Thread(target=self._keepalive, daemon=True)
        keepalive_thread.start()

        self.sock.settimeout(1.0)
        while self.running:
            try:
                conn, addr = self.sock.accept()
                print(f"Player connected from {addr}")
                player_thread = threading.Thread(target=self._handle_player, args=(conn, addr))
                player_thread.daemon = True
                player_thread.start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _discovery_listener(self):
        """Respond to slimproto discovery broadcasts.

        Discovery traffic is throttled and aggregated so a misbehaving device
        cannot spin up a request/response feedback loop: at most one response
        per source per throttle window, a global response cap per summary
        window, and a single summary log line per window instead of one print
        per packet. Only this thread touches the stats dicts, so no locking.
        """
        # Response bytes are identical every time; build them once.
        version = "9.1.0"
        http_port = str(self.http_port)
        cli_port = "9090"
        response = b"e"
        response += b"VERS" + bytes([len(version)]) + version.encode()
        response += b"JSON" + bytes([len(http_port)]) + http_port.encode()
        response += b"CLIP" + bytes([len(cli_port)]) + cli_port.encode()

        SUMMARY_INTERVAL = 30.0   # seconds between aggregate log lines
        RESPONSE_INTERVAL = 12.0  # at most one response per source per this window
        MAX_RESPONSES = 20        # global response cap per SUMMARY_INTERVAL window

        last_response = {}        # source IP -> monotonic time of last response
        window_start = time.monotonic()
        req_count = 0             # requests received in the current window
        resp_count = 0            # responses sent in the current window
        sources = {}              # source IP -> request count in current window

        while self.running:
            now = time.monotonic()
            if now - window_start >= SUMMARY_INTERVAL:
                if req_count or resp_count:
                    print(f"Discovery: {req_count} requests from {len(sources)} "
                          f"sources, {resp_count} responses sent in last "
                          f"{SUMMARY_INTERVAL:.0f}s")
                window_start = now
                req_count = 0
                resp_count = 0
                active = set(sources)
                sources = {}
                # Drop throttle state for sources that went quiet so a device
                # that returns later is not stuck throttled forever.
                last_response = {ip: t for ip, t in last_response.items()
                                 if ip in active}

            try:
                _, addr = self.udp_sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break

            src_ip = addr[0]
            req_count += 1
            sources[src_ip] = sources.get(src_ip, 0) + 1

            # Throttle per source: one response per RESPONSE_INTERVAL is enough
            # for a roving player to discover us, and it breaks the feedback loop.
            if resp_count >= MAX_RESPONSES:
                continue
            if now - last_response.get(src_ip, -RESPONSE_INTERVAL) < RESPONSE_INTERVAL:
                continue
            try:
                self.udp_sock.sendto(response, addr)
            except OSError:
                continue
            last_response[src_ip] = now
            resp_count += 1

    def _handle_player(self, conn, addr):
        """Handle a single player connection."""
        player = {"conn": conn, "addr": addr}
        self.players.append(player)

        mac_hex = None  # for sync-start cleanup in finally

        try:
            # Read the HELO packet
            helo_data = self._read_packet(conn)
            if not helo_data or not helo_data.startswith(b"HELO"):
                print(f"  Invalid HELO from {addr}")
                return

            # HELO_packet: opcode(4) length(4) deviceid(1) revision(1) mac[6] uuid[16]
            mac = helo_data[10:16]  # MAC is bytes 10-16
            # Classify this player for the optional L/R split: if its HELO MAC
            # matches a configured raopbridge.xml <mac>, send it /stream/L or
            # /stream/R so each HomePod plays only its own channel.
            mac_hex = mac.hex()  # lowercase 12-hex, matches _normalize_mac
            channel_suffix = ""
            if self.left_mac and mac_hex == self.left_mac:
                channel_suffix = "/L"
            elif self.right_mac and mac_hex == self.right_mac:
                channel_suffix = "/R"
            label = "LEFT" if channel_suffix == "/L" else ("RIGHT" if channel_suffix == "/R" else "stereo")
            print(f"  Player MAC: {':'.join(f'{b:02x}' for b in mac)} -> {label}")

            # Send SETD for device name
            conn.sendall(pack_setd_name(self.device_name))
            print(f"  Sent SETD")

            # Enable audio output
            conn.sendall(pack_aude(True))
            print(f"  Sent AUDE")

            from urllib.parse import urlparse
            audio_url = self.audio_url or f"http://{self._get_local_ip()}:{self.http_port}/stream"
            parsed = urlparse(audio_url)
            server_ip = parsed.hostname
            server_port = parsed.port or 80
            base = (parsed.path or "/stream").rstrip("/") or "/stream"

            # Sync-start: in L/R mode, hold each player's strm until BOTH have
            # connected so the two RAOP sessions begin together. The two
            # players connect ~15s apart; starting them separately leaves one
            # HomePod on a different playout-latency schedule (audible L/R
            # desync). If the partner never connects, the first player starts
            # alone after STRM_SYNC_TIMEOUT.
            if self.left_mac and self.right_mac and channel_suffix:
                with self._strm_cond:
                    if self._strm_solo:
                        # A member already timed out and started alone; a late
                        # partner starts immediately rather than waiting again.
                        to_start = {mac_hex: {"conn": conn, "suffix": channel_suffix}}
                    else:
                        self._strm_pending[mac_hex] = {"conn": conn, "suffix": channel_suffix}
                        if self.left_mac in self._strm_pending and self.right_mac in self._strm_pending:
                            to_start = dict(self._strm_pending)
                            self._strm_pending.clear()
                            self._strm_cond.notify_all()
                        else:
                            to_start = {}
                            self._strm_cond.wait(timeout=STRM_SYNC_TIMEOUT)
                            if mac_hex in self._strm_pending:
                                to_start = {mac_hex: self._strm_pending.pop(mac_hex)}
                                self._strm_solo = True
                for info in to_start.values():
                    path = base + info["suffix"]
                    c = info["conn"]
                    c.sendall(pack_strm_start(server_ip, server_port, path))
                    print(f"  Sent strm: {server_ip}:{server_port}{path}")
                    c.sendall(pack_audg(TARGET_VOLUME_PERCENT, TARGET_VOLUME_PERCENT))
                    print(f"  Sent audg (volume {TARGET_VOLUME_PERCENT}%) after strm 's'")
            else:
                # Not an L/R pair member (or no L/R split): start immediately.
                request_path = base + channel_suffix
                conn.sendall(pack_strm_start(server_ip, server_port, request_path))
                print(f"  Sent strm: {server_ip}:{server_port}{request_path}")
                # Volume: send AUDG immediately after strm 's', exactly like
                # LMS (gainL 63 -> -11.7 dB). Re-asserted by the keepalive.
                conn.sendall(pack_audg(TARGET_VOLUME_PERCENT, TARGET_VOLUME_PERCENT))
                print(f"  Sent audg (volume {TARGET_VOLUME_PERCENT}%) after strm 's'")

            # Keep the connection alive - read STAT messages and ignore them
            while self.running:
                data = self._read_packet(conn, timeout=5.0)
                if data is None:      # read timeout — keep waiting
                    continue
                if not data:          # EOF — player disconnected
                    break
                opcode = data[:4]
                print(f"  Received: {opcode}")

        except (ConnectionResetError, BrokenPipeError, socket.timeout):
            pass
        finally:
            # Drop this player from the sync-start wait set so a dead member
            # doesn't make its partner wait the full timeout.
            if mac_hex:
                with self._strm_cond:
                    self._strm_pending.pop(mac_hex, None)
            conn.close()
            if player in self.players:
                self.players.remove(player)
            print(f"Player {addr} disconnected")

    def _keepalive(self):
        """Keep connections alive and re-assert the target volume.

        A working LMS session keeps sending SET_PARAMETER volume to both
        HomePods of a pair every few seconds (driven by the volume feedback
        loop). Without that, the pair occasionally self-resets one device to
        volume 0 (mute). We replicate it by pushing the target AUDG
        periodically; a device that gets a steady non-muted volume command
        keeps playing. It also doubles as the TCP keepalive.
        """
        while self.running:
            time.sleep(4)
            for player in self.players:
                try:
                    player["conn"].sendall(pack_audg(TARGET_VOLUME_PERCENT, TARGET_VOLUME_PERCENT))
                except OSError:
                    pass

    def _cli_server(self):
        """Minimal CLI server (port 9090) so squeezelite players can complete
        reconnects. Real LMS uses this to expose player info; here we just
        accept the connection and echo commands back.

        One thread per connection: squeezelite's cli_send_cmd() waits (up to
        500ms) for its URL-encoded command to appear in the response, and both
        players query the CLI concurrently. A single-threaded echo that serves
        one connection at a time starves the other player's queries (they all
        time out), which stalls its stream thread and destabilizes the RAOP
        session. Echoing each received line satisfies the check instantly.
        """
        cli_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cli_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cli_sock.bind((self.listen_ip, 9090))
        cli_sock.listen(5)
        cli_sock.settimeout(1.0)
        print(f"CLI server listening on {self.listen_ip}:9090")

        def _echo(conn, addr):
            try:
                conn.settimeout(1.0)
                while self.running:
                    try:
                        data = conn.recv(1024)
                        if not data:
                            break
                        conn.sendall(data)
                    except socket.timeout:
                        continue
            except (OSError, ConnectionError):
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

        while self.running:
            try:
                conn, addr = cli_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=_echo, args=(conn, addr), daemon=True).start()

    def _get_local_ip(self):
        """Get the local IP address."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "127.0.0.1"

    def _read_packet(self, conn, timeout=5.0):
        """Read a slimproto packet: 8-byte header + payload.

        Returns None on read timeout (caller keeps waiting), b"" on EOF
        (peer closed the connection — caller must break out of its loop),
        else the full packet bytes. Distinguishing the two matters: recv
        returning b"" happens instantly, so treating it like a timeout made
        the per-player loop spin at 100% CPU on a dead connection.
        """
        conn.settimeout(timeout)
        try:
            header = conn.recv(8)
            if len(header) < 8:
                return b""  # EOF: peer closed the connection

            opcode = header[:4]
            length = struct.unpack(">I", header[4:8])[0]
            if length > 65536:
                return b""

            # Read the rest of the packet
            payload = b""
            while len(payload) < length:
                chunk = conn.recv(length - len(payload))
                if not chunk:
                    return b""  # EOF mid-packet
                payload += chunk

            return header + payload
        except socket.timeout:
            return None

    def stop(self):
        """Stop the server."""
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass
        for p in self.players:
            try:
                p["conn"].close()
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description="Minimal slimproto server for squeeze2raop")
    parser.add_argument("--port", type=int, default=3483)
    parser.add_argument("--http-port", type=int, default=9000)
    parser.add_argument("--name", default="HomePod Bridge")
    parser.add_argument("--left-mac", default=os.environ.get("HOMEPOD_LEFT_MAC", ""),
                        help="HomePod <mac> that should play only the left channel")
    parser.add_argument("--right-mac", default=os.environ.get("HOMEPOD_RIGHT_MAC", ""),
                        help="HomePod <mac> that should play only the right channel")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    server = SlimprotoServer(port=args.port, http_port=args.http_port,
                             device_name=args.name,
                             left_mac=args.left_mac, right_mac=args.right_mac)
    server.start()


if __name__ == "__main__":
    main()
