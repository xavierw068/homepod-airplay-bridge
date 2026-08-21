#!/usr/bin/env python3
"""
DACP broadcast + command server for the HomePod Bridge relay path.

Advertises a DACP (Digital Audio Control Protocol) service over zeroconf
(mDNS) so a HomePod sees a controllable "iTunes" host, then listens on the
DACP port and answers the HomePod's ActiveRemote/control requests with a
200 (same as squeeze2raop does for volume/property queries).

Used by the relay path (start-full.sh): cliraop needs an active DACP
broadcast so the HomePod accepts the session.

Requires the `zeroconf` package and UDP port 5353 free (stop avahi).
Usage:
    python3 dacp_broadcast.py [--ip SERVER_IP] [--port 40099]
"""

import argparse
import os
import socket
import threading
import time

from zeroconf import Zeroconf, ServiceInfo

DACP_ID = "1A2B3D4EA1B2C3D4"


def _get_local_ip():
    """Best-effort local IP discovery (UDP connect trick)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def dacp_server(port):
    """Listen for HomePod connecting to the DACP port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(10)
    print(f"DACP server listening on :{port}", flush=True)
    while True:
        try:
            conn, addr = s.accept()
            print(f"HomePod connected DACP: {addr}", flush=True)
            conn.settimeout(30)
            # Keep the connection alive and respond to ActiveRemote requests
            while True:
                try:
                    data = conn.recv(4096)
                    if not data:
                        break
                    print(f"DACP request: {data[:150]}", flush=True)
                    # Respond 200 (like squeeze2raop does for volume/property queries)
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Type: text/parameters\r\n\r\n")
                except socket.timeout:
                    print("DACP connection idle, keeping alive", flush=True)
                    continue
                except Exception as e:
                    print(f"DACP handler err: {e}", flush=True)
                    break
            conn.close()
        except Exception as e:
            print(f"DACP accept err: {e}", flush=True)
            time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description="Broadcast DACP service via zeroconf")
    parser.add_argument("--ip", default=os.environ.get("SERVER_IP") or _get_local_ip(),
                        help="local IP to advertise (default: $SERVER_IP or auto-detect)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("DACP_PORT", "40099")),
                        help="DACP port (default: $DACP_PORT or 40099)")
    args = parser.parse_args()

    threading.Thread(target=dacp_server, args=(args.port,), daemon=True).start()

    zc = Zeroconf()
    info = ServiceInfo(
        "_dacp._tcp.local.",
        f"iTunes_Ctrl_{DACP_ID}._dacp._tcp.local.",
        addresses=[socket.inet_aton(args.ip)],
        port=args.port,
        properties={
            b"txtvers": b"1",
            b"Ver": b"131075",
            b"DbId": b"63B5E5C0C201542E",
            b"OSsi": b"0x1F5",
        },
    )
    zc.register_service(info)
    print(f"DACP broadcast registered: iTunes_Ctrl_{DACP_ID} on {args.ip}:{args.port}", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        zc.unregister_service(info)
        zc.close()


if __name__ == "__main__":
    main()
