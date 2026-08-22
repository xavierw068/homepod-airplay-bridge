# HomePod Bridge

Route audio from a Linux desktop (PipeWire) to Apple HomePods over AirPlay — **without a full Logitech Media Server**.

把 Linux 桌面(PipeWire)的音频通过 AirPlay 路由到 Apple HomePod——**不需要完整的 Logitech Media Server**。

The LMS side is replaced by two small Python daemons (a GStreamer/parec capture server and a minimal SlimProto server); a vendored `squeeze2raop` bridge plays the resulting stream to the HomePods. Any app that plays to a PipeWire virtual sink gets captured and broadcast to your HomePod(s) — including stereo pairs.

## Features / 特性

- **No LMS required** — replaces Logitech Media Server with two lightweight Python daemons
- **Two independent data paths** — a primary SlimProto + HTTP path, and a relay path for DACP command-and-control testing
- **Stereo pair support** — each HomePod gets its own RAOP session, matched to how LMS syncs a group
- **Optional true L/R split** — configure each HomePod's `<mac>` (in `config.env`) so one plays only the left channel and the other only the right; empty = full stereo on both
- **Any app captured** — point the default PipeWire sink at the bridge and everything (Firefox, etc.) follows
- **EQ/DSP integration** — optional EasyEffects chain between apps and the bridge sink
- **ALAC/RTP** — HomePods reject bare PCM, so audio is ALAC-encoded (`squeeze2raop -c alac` / `cliraop -a`)

## Architecture / 架构

```
                       ┌──────────────────────────────────────────────────┐
                       │  this Linux box                                   │
                       │                                                  │
  apps/browser ──────► │  (PipeWire) easyeffects_sink ─► DSP chain        │
                       │                         │                        │
                       │                         ▼                        │
                       │               homepod-bridge-sink (virtual)      │
                       │                         │  monitor               │
                       │                         ▼                        │
                       │            audio_stream_server.py (HTTP :9000)   │
                       │                         │                        │
                       │  slimproto_server.py ──►│   strm → GET /stream    │
                       │       (:3483)           │                        │
                       └─────────┬───────────────┴────────────┬───────────┘
                                 │                            │
                                 ▼                            ▼
                         squeeze2raop (-Z)             cliraop  (relay path)
                                 │                            │
                                 ▼                            ▼
                        HomePod 1 & 2 (RAOP/ALAC)     HomePod (RAOP/ALAC)
```

Two data paths (both start playback on the HomePods):

1. **HTTP-stream path (default)** — `start.sh`
   `src/audio_stream_server.py` captures the PipeWire sink monitor via GStreamer (`pulsesrc`, S16LE/44100/stereo, 352-frame buffers) and serves infinite WAV-header PCM on `http://…:9000/stream`. `src/slimproto_server.py` is a minimal SlimProto TCP server (port 3483) that skips discovery/HELO and immediately issues a `strm` command telling each connected player to fetch that HTTP URL. The player is `squeeze2raop` run with `-Z` (no avahi), pointing at `SERVER_IP:3483`.

2. **Relay path** — `start-full.sh`
   `relay.py` (HTTP → `cliraop` stdin) or `relay_parec.py` (PipeWire monitor via `parec` → `cliraop`). Used to test DACP command-and-control first; `cliraop` needs a DACP broadcast active (`src/dacp_broadcast.py`, python-zeroconf, UDP 5353 free → avahi must be stopped).

## Components / 组件

| Path | File | Role |
|------|------|------|
| `src/` | `audio_stream_server.py` | GStreamer capture → HTTP (the working capture; per-client broadcast queues) |
| `src/` | `slimproto_server.py` | Minimal SlimProto server issuing `strm` + volume keepalive |
| `src/` | `audio_server_parec.py` | Alternative HTTP server using `parec` instead of GStreamer |
| `src/` | `audio_capture.py` | **Legacy** capture experiment (UNIX-socket/WAV, uses `pipewiresrc` — grabs the wrong node on some setups) |
| `src/` | `dacp_broadcast.py` | DACP zeroconf broadcast + command server (relay path only) |
| root | `relay.py`, `relay_parec.py` | Frame-aligned relays feeding `cliraop` (1408-byte blocks) |
| root | `start.sh` / `start-full.sh` | Launchers for the two paths |
| root | `homepod-audio.sh` | Daily output switching + volume (`homepod` / `speaker` / `toggle` / `status` / `vol`) |
| root | `easyeffects-switch.sh` | Save/switch EasyEffects EQ snapshots |
| `config/` | `raopbridge.xml.example` | `squeeze2raop` config template |

## Prerequisites / 前置条件

- Linux with **PipeWire** (PulseAudio-compatible layer) and `pactl`
- **GStreamer** with Python bindings (`gi`, `Gst`, `GstApp`) — for `audio_stream_server.py`
- `parec` (pulseaudio-utils) — for the parec-based variants
- `zeroconf` (python package) — only for the relay path (`start-full.sh`)
- A PipeWire **virtual sink** (default name `homepod-bridge-sink`) — see Setup
- `squeeze2raop` binary — build from upstream, see below

## Setup / 安装

### 1. Configuration

```bash
cp config.env.example config.env
$EDITOR config.env        # fill in your SERVER_IP, HOMEPOD_IP, sink names, binary paths
cp config/raopbridge.xml.example config/raopbridge.xml
$EDITOR config/raopbridge.xml   # fill in your HomePod udn/name/mac in the <device> blocks
```

`config.env` and `config/raopbridge.xml` are gitignored — your local values stay local.

### 2. Create the virtual sink

```bash
pactl load-module module-null-sink sink_name=homepod-bridge-sink
# persist across reboots (PipeWire): add to ~/.config/pipewire/pipewire.conf.d/ or use your WM autostart
```

### 3. Build `squeeze2raop`

This repo makes **no modifications** to the upstream source (verified against upstream `master`). Clone and build the upstream bridge:

```bash
git clone --recursive https://github.com/philippe44/lms-raop
cd lms-raop/application
make            # produces squeeze2raop in squeeze2raop/bin/
```

Put the resulting binary where `config.env`'s `SQUEEZE2RAOP_BIN` points (default `/tmp/squeeze2raop`). If you run several instances or your HomePod ignores the default RAOP identity, follow upstream's docs for a custom device-id and adjust the binary name accordingly.

For the relay path you also need `cliraop` (an AirPlay/RAOP ALAC client) — place it where `CLIRAOP_BIN` points (default `/tmp/cliraop`).

### 4. (Optional) EasyEffects EQ chain

Route apps through `easyeffects_sink` → DSP chain (Equalizer → Bass Enhancer → Loudness → Compressor → Limiter) → `homepod-bridge-sink`, then use `easyeffects-switch.sh switch <name>` to load saved EQ snapshots.

## Running / 运行

```bash
# Primary path: SlimProto + HTTP → HomePod(s)
bash start.sh

# Relay path (DACP test): stops avahi, starts DACP broadcast + audio server + relay
bash start-full.sh

# Daily controls: switch output, status, volume
homepod-audio.sh homepod|speaker|toggle|status|vol up|down|NN
```

Logs are written under `/tmp/`: `audio-server.log`, `slimproto-server.log`, `s2r.log`, `dacp-bcast.log`, `relay.log`.

## Volume management / 音量

This is the part that took the longest to get right.

- **RAOP side is held constant at ~50% (−11.7 dB)** by the `AUDG` keepalive in `slimproto_server.py` (`TARGET_VOLUME_PERCENT`). This matches what a working LMS session drives a stereo pair at. Sending 0 dB (=100%) makes the pair coordinator push the secondary HomePod to volume 0 (mute).
- **Loudness is tuned on the PipeWire sink**: `pactl set-sink-volume homepod-bridge-sink <pct>` (or `homepod-audio.sh vol up/down/NN`).
- **`pactl` trap**: a bare number is treated as a `/65536` linear value (`55 ≈ 0%`) — always use `NN%`.

## L/R channel split / 左右声道分离

By default every HomePod gets the **full stereo** stream and each unit mono-downmixes it, so left- and right-panned content is audible from both speakers. To get a true stereo image, set which HomePod plays which channel:

```bash
# config.env — use the <mac> values from config/raopbridge.xml (colons optional, case-insensitive)
HOMEPOD_LEFT_MAC=aa:aa:11:96:29:f0
HOMEPOD_RIGHT_MAC=aa:aa:d3:ab:07:18
```

How it works: `slimproto_server.py` matches each player's HELO MAC (the `<mac>` in `raopbridge.xml`) against these two values and issues `strm` with `/stream/L` or `/stream/R`. `audio_stream_server.py` serves those paths with the chosen channel **duplicated into a stereo frame** (1408 bytes, alignment untouched), so each HomePod's mono-downmix yields exactly one channel. Any other path — including `/stream` (the relay scripts) — still serves full stereo.

- Both MACs set → true L/R split. One set → only the matching player is split, the other stays stereo (a startup warning is logged). Neither set → old behavior (full stereo everywhere).
- L/R is just placement — swap the two values to reverse the sides.
- Requires non-zero, **unique** `<mac>` values in `raopbridge.xml`; a zero/duplicate MAC makes squeeze2raop auto-generate one and the match silently fails.
- Verify with a distinct-tone-per-channel test file (e.g. left 440 Hz, right 880 Hz) routed through `homepod-bridge-sink`; check `/tmp/slimproto-server.log` shows `Player MAC: … -> LEFT` / `-> RIGHT`.

## Troubleshooting / 疑难排查

- **Two players split the data rate → silence.** Never share one queue between clients. `audio_stream_server.py` gives every HTTP client its own queue fed by a single dispatcher thread (that is what `_broadcast`/`_dispatcher` do). If you fork the server, keep that design.
- **352-frame alignment.** AirPlay ALAC uses 352 frames/packet; 1408 bytes = 352 frames × 2ch × 2 bytes. Keep HTTP reads aligned to 1408 bytes or PCM frames slip. The relay scripts exist because HTTP writes arrive misaligned to readers.
- **HomePod stereo pair rejects bare PCM** — must be ALAC-encoded (`-c alac` on squeeze2raop, `-a` on cliraop).
- **`autostart=1`, not a `strm u` coordinated wait.** Each player starts as soon as it has buffered; the two sessions don't need to start at the same wall-clock moment — RAOP NTP sync aligns them. `autostart=0` + a far-future `strm u` was tried and left the pair silent.
- **AUDG packet format.** squeezelite's `audg_packet` is 22 bytes big-endian (`opcode(4) old_gainL(4) old_gainR(4) adjust(1) preamp(1) gainL(4) gainR(4)`), and `process_audg()` derives volume from `old_gainL` only. Send the wrong size/fields and you get a 0-volume (silence) request.
- **A wedged HomePod refuses port 7000 entirely** (TCP SYN-SENT, no ESTAB) after heavy session churn. Fix: power-cycle the HomePod. Symptoms mimic a "pair coordination failure" — check `ss -tn | grep :7000` and `bash -c 'echo > /dev/tcp/<ip>/7000'` before blaming the protocol.
- **pyatv secondary-volume reads 0.0** even while that HomePod plays fine — don't use pyatv secondary-volume to conclude a mute.

## License / 许可证

[GPL-3.0](./LICENSE).

This project is the thin Python/shell layer; the audio transport relies on the upstream
[`philippe44/lms-raop`](https://github.com/philippe44/lms-raop) (GPL) bridge and `cliraop`.
