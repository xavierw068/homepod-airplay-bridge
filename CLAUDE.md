# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

HomePod Bridge — routes audio from this Linux box (PipeWire) to Apple HomePods over AirPlay, without a full Logitech Media Server. The "server" side replaces LMS with two small Python daemons, and a `squeeze2raop` binary plays the resulting stream to the HomePods.

All machine-specific values (server IP, HomePod IP, sink name, binary paths, ports) live in `config.env` (copy from `config.env.example`). HomePod identity (name/MAC/UDN) lives in `config/raopbridge.xml` (copy from `config/raopbridge.xml.example`). Both are gitignored. **Never hardcode machine values in code — this repo is public.**

The repo does not vendor `squeeze2raop`: the `squeeze2raop-src/` directory (the philippe44/lms-raop tree, built with `make`) is kept locally as a build dependency but excluded from git. See README for how to fetch and build it.

## Components

Two independent data paths (both start HomePod playback via `squeeze2raop`):

1. **HTTP-stream path** (the `start.sh` default): `src/audio_stream_server.py` captures the PipeWire sink monitor via GStreamer (`pulsesrc`, S16LE/44100/stereo — 352-frame buffers) and serves infinite WAV-header PCM on `http://…:9000/stream`. `src/slimproto_server.py` is a minimal SlimProto TCP server on port 3483 that dispenses with discovery+HELO and immediately issues a `strm` command telling the connected player to fetch that HTTP URL. The player is a `squeeze2raop` binary run in `-Z` (no avahi) mode, config in `config/raopbridge.xml`, pointing at `$SERVER_IP:$SLIMPROTO_PORT`.
2. **Relay path**: `relay.py` (HTTP → `cliraop` stdin) or `relay_parec.py` (PipeWire monitor via `parec` → cliraop). Used to test DACP command-and-control first; cliraop needs a DACP broadcast active (`src/dacp_broadcast.py`, python-zeroconf, needs port 5353 free → avahi must be stopped).

`squeeze2raop` must be invoked with `-a 40000:100` (DACP port range), which is why the DACP port 40099 sits in the broadcast script.

## How to run

```bash
# Full stack: stops avahi, starts DACP broadcast + audio server + relay → HomePod
bash start-full.sh
# SlimProto stack: kills old python/squeeze2raop, starts audio server + slimproto server + squeeze2raop
bash start.sh
```

Both scripts require `config.env` (copy from `config.env.example` and fill in your IPs/sink/binary paths). Components write logs under `/tmp/`: `audio-server.log`, `slimproto-server.log`, `s2r.log`, `dacp-bcast.log`, `relay.log`. The `squeeze2raop`/`cliraop` binaries live where `config.env` points them (this box keeps them in `/tmp`, copied from a build machine); the repo ships no built binaries.

Daily use (routing + volume): `homepod-audio.sh homepod|speaker|toggle|status|vol up|down|NN`. The default sink is `homepod-bridge-sink` (`$BRIDGE_SINK`), so any local player/browser (Firefox etc.) that follows the default sink is captured and played to both HomePods. Volume is adjusted on the PipeWire sink (`pactl set-sink-volume homepod-bridge-sink NN%`) — NOTE: `pactl` treats a bare number as /65536 linear (55 ≈ 0%), so always use `NN%` (the script handles this). The RAOP-side volume is fixed at 50% (-11.7 dB) by the AUDG keepalive and should not be changed via AirPlay commands.

EQ/DSP: EasyEffects 8.2.8 routes apps through `easyeffects_sink` → DSP chain (Equalizer→Bass Enhancer→Loudness→Compressor→Limiter, stored in `~/.config/easyeffects/db/*.rc`) → `homepod-bridge-sink`. Config snapshots live in `~/.config/easyeffects.snapshots/` (`jblgo5` = original small-speaker V-curve, `homepod` = flattened for HomePod's real bass: band0 shelf +3.5→+1.5 dB, mids cuts removed, treble boost removed, bass enhancer 6→2, `tube` = 胆机/valve-amp voicing: warm low-mid EQ bump + slow soft-knee compressor + Calf Exciter harmonics + ZaMaximX2 soft-clip ceiling, chain `equalizer→bass_enhancer→loudness→compressor→exciter→maximizer`). Switch with `easyeffects-switch.sh switch|save <name>` (restarts EasyEffects). Note: launching EasyEffects from a non-GUI shell requires the sway session env (`WAYLAND_DISPLAY=wayland-1`), else it core-dumps silently. Editing the `.rc` files while EasyEffects runs is lost on exit — always kill first (the script does this).

Prereqs: GStreamer Python bindings (`gi`), `zeroconf` (for the relay route), `parec`, and the `homepod-bridge-sink` PipeWire virtual sink. `start-full.sh` stops avahi because python-zeroconf needs UDP 5353.

## Key invariants

- **352-frame / 1408-byte alignment matters.** AirPlay ALAC uses 352 frames per packet; 1408 = 352 frames × 2ch × 2bytes. `squeeze2raop`/`cliraop` read exactly these blocks, so any code pulling the HTTP stream must keep reads aligned to 1408 bytes or PCM frames slip. The relay scripts exist precisely because `audio_stream_server.py` buffers are 352-sample bound but HTTP/`wfile` writes arrive misaligned to readers.
- **HomePod stereo pair rejects bare PCM** — RTP must be ALAC-encoded. Hence `-c alac` on squeeze2raop and `-a` on cliraop.
- **HomePod pair plays BOTH at moderate negative dB** (e.g. -12 dB). The old claim "pair mutes below 0 dB" was a misdiagnosis. Verified against a working LMS session: LMS drives the pair at ~50% (index 63 → -11.7 dB) and both play. `slimproto_server.py` sends `TARGET_VOLUME_PERCENT` (default 50) via `pack_audg()` right after `strm s` and re-asserts it every 4 s (the keepalive), which is the volume management that keeps the pair audible. Loudness is fine-tuned with the PipeWire sink: `pactl set-sink-volume homepod-bridge-sink <pct>`.
- **Two independent RAOP sessions are how the pair plays** (matches LMS's sync group). Each HomePod gets its own session; the pair does NOT internally split a single session to the primary (tested — a session to the primary alone plays only the primary).
- **pyatv volume of the paired SECONDARY reads 0.0** even while it plays fine (verified during a single-HomePod session: it sounded, pyatv said 0). Do not use pyatv secondary-volume to conclude a mute.
- **Idle HTTP silence is served at the real-time rate** (one 1408-byte block per ~8 ms, paced by wall-clock) — serving it slower starves the player and pushes the playhead seconds behind real-time.
- **Optional L/R split duplicates a channel into both frame slots — it never re-formats the stream.** With `HOMEPOD_LEFT_MAC`/`HOMEPOD_RIGHT_MAC` set in config.env, `slimproto_server.py` matches each player's HELO MAC (the `<mac>` in `raopbridge.xml`) and issues `strm` for `/stream/L` or `/stream/R`; `audio_stream_server.py` serves those paths by copying the chosen channel into both slots of every 1408-byte stereo frame (`_dupe_channel`). The stream stays stereo S16LE/44100, so alignment, the WAV header, `pcm_channels = b"2"`, and ALAC encoding are untouched; each HomePod's internal mono-downmix then reproduces exactly one channel. Unmatched MACs / empty vars → full stereo (old behavior; the relay path GETs `/stream` and is unaffected). config.env vars reach slimproto as explicit CLI args (`--left-mac`/`--right-mac`) because `start.sh` sources config.env without `export`.

## Gotchas already discovered

- **`audio_stream_server.py` MUST broadcast to every HTTP client, not share one queue.** A single shared `queue.Queue` serving both squeeze2raop players splits the data rate (~1/3 each with 2 players), so the stream's playhead falls ~8 s behind wall-clock and no sound is heard (all audio arrives after the clip). The fix: one dispatcher thread reads the capture queue and copies each block into every client's own queue (`_broadcast` / `_dispatcher`). Verified: two clients each get ~99% of the real-time rate (176400 B/s for S16LE/44100/stereo).
- GStreamer `pipewiresrc` grabs the wrong PipeWire node on this box (`target-object` matching picks the mic); `pulsesrc` + `sinkname.monitor` works.
- `src/audio_capture.py` is the older experiment (UNIX-socket / WAV output, uses `pipewiresrc`); `audio_stream_server.py` is the working HTTP server.
- `audio_server_parec.py` is a third variant (parec-based HTTP server); relay_parec bypasses HTTP entirely.
- **The upstream C bridge is NOT modified.** `squeeze2raop-src/application/squeeze2raop/` (including `config_raop.c`) is byte-identical to philippe44/lms-raop `master`. If you ever rebuild the binary, clone upstream as-is; do not expect local tweaks here. (An earlier note claimed config_raop.c tweaks lived here — that never landed.)

### squeezelite/slimproto quirks (found the hard way)

- **AUDG packet format**: squeezelite's `audg_packet` is 22 bytes (`opcode(4) old_gainL(4) old_gainR(4) adjust(1) preamp(1) gainL(4) gainR(4)`), big-endian. `process_audg()` has a bug — it computes volume from `old_gainL` only (ignores gainL/gainR), and `LMSVolumeMap[gain]` maps an index 0..128 to percent 0..100. Sending the wrong size/fields results in a 0-volume (silence) request.
- **Coordinated start = `autostart=1`, not a `strm u` wait.** LMS's actual `strm s` is `autostart: 1` (verified in a working raopbridge log). Each player starts as soon as it has buffered; the two sessions need NOT begin at the same wall-clock moment — the RAOP NTP sync aligns them during playback (the reference LMS's own two sessions started ~0.8 s apart and the pair played them fine). Earlier attempts used `autostart=0` + `strm u` with a far-future jiffies and left the pair silent (the long coordinated wait makes the HomePod time out the session). `slimproto_server.py` therefore sends `autostart=1` and no `u`.
- **slimproto HELO MAC offset**: MAC lives at `helo_data[10:16]` (after `opcode(4)+length(4)+deviceid(1)+revision(1)`), not byte 12.
- **L/R routing keys on the HELO MAC**, which squeeze2raop takes from the `<mac>` in `raopbridge.xml`. A zero/duplicate `<mac>` triggers squeeze2raop's MAC auto-generation and the match silently fails — the split requires non-zero, unique `<mac>` values.
- **A wedged HomePod refuses port 7000 entirely** (TCP SYN-SENT, no ESTAB) after heavy session churn. Fix: power-cycle the HomePod. Symptoms mimic "pair coordination failure" — check `ss -tn | grep :7000` and `bash -c 'echo > /dev/tcp/<ip>/7000'` before blaming the protocol.
- **The two players connect SLOWLY and sometimes one never does.** After `start.sh`, player 2 often connects 15-60s (or minutes) after player 1, and a player can drop its HTTP fetch / RAOP session and not retry — so for a while only one HomePod plays. `ss -tn state established | grep :7000` must show BOTH HomePods before judging; `grep -c 'raop connected' /tmp/s2r.log` on the latest run helps. A player stuck at `output_raop_thread_init` / `raop connecting` before ever touching slimproto or :9000 is a HomePod/session problem, not the bridge code. Reliable cure after heavy churn: power-cycle both HomePods, then `bash start.sh`.
- **CLI server must echo, not just stay mute.** `_cli_server()` had two bugs. (1) Its `finally` called `conn.close()` with `conn` unbound on `accept()` timeout, killing the 9090 thread ~1 s after every start — the old source of repeated `cli unable to connect to server with cli` in s2r.log; that failure mode was actually harmless (players fell back to no-CLI). (2) Once the crash was fixed, a mute CLI server was WORSE: squeezelite's `cli_send_cmd()` (main.c:251) waits up to 500 ms for its URL-encoded command to appear in the response, so a never-replying server made the player's stream thread stall on every `status`/`time` query, destabilizing the RAOP session (started dropping one HomePod). The server now **echoes each command back**, which satisfies the check instantly. If `Timeout waiting for CLI reponse` reappears in s2r.log, the echo is broken again.
