#!/bin/bash
# HomePod Bridge 完整启动脚本 (relay 路径 / relay path)
# 启动: DACP 广播 + audio_server + relay → HomePod
# 用途:测试 DACP 命令-控制链路(relay → cliraop)。主路径请用 start.sh。
#
# 注意:python-zeroconf 需要 UDP 5353,本脚本会停止 avahi。
# 用法/Usage: bash start-full.sh(需先 cp config.env.example config.env 并填写)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 加载本地配置 (load local config)
if [ ! -f "$SCRIPT_DIR/config.env" ]; then
  echo "缺少 config.env: cp config.env.example config.env 并填写" >&2
  exit 1
fi
. "$SCRIPT_DIR/config.env"

DACP_SCRIPT="$SCRIPT_DIR/src/dacp_broadcast.py"
RELAY_SCRIPT="$SCRIPT_DIR/relay.py"
AUDIO_SCRIPT="$SCRIPT_DIR/src/audio_stream_server.py"
PYZEROCONF="${PYZEROCONF_PYTHON:-python3}"

echo "=== 清理旧进程 (cleanup) ==="
for pat in "dacp_broadcast" "relay.py" "audio_stream_server" "cliraop" "squeeze2raop" "avahi-publish"; do
  for pid in $(pgrep -f "$pat" 2>/dev/null); do kill -9 $pid 2>/dev/null || true; done
done
sleep 1

echo "=== 1. 停 avahi（python-zeroconf 需要 5353）==="
sudo systemctl stop avahi-daemon avahi-daemon.socket 2>/dev/null || true
sleep 1

echo "=== 2. 启动 DACP 广播 ==="
setsid "$PYZEROCONF" "$DACP_SCRIPT" --ip "$SERVER_IP" --port "$DACP_PORT" \
    > /tmp/dacp-bcast.log 2>&1 < /dev/null &
sleep 4
grep -q "registered" /tmp/dacp-bcast.log && echo "  DACP 广播 OK" || echo "  DACP 广播失败"

echo "=== 3. 启动 audio_stream_server（HTTP $HTTP_PORT）==="
setsid python3 "$AUDIO_SCRIPT" --http-port "$HTTP_PORT" --sink "$BRIDGE_SINK" \
    > /tmp/audio-server.log 2>&1 < /dev/null &
sleep 3
ss -tln | grep -q "$HTTP_PORT" && echo "  audio server OK" || echo "  audio server 失败"

echo "=== 4. 等待 HomePod 连接 DACP ==="
DACP_OK=0
for i in $(seq 1 20); do
  if ss -tn | grep -q "$DACP_PORT"; then echo "  HomePod 已连 DACP (${i}s)"; DACP_OK=1; break; fi
  sleep 1
done
[ $DACP_OK -eq 0 ] && echo "  警告: HomePod 未连接 DACP"

echo "=== 5. 启动 relay（HTTP → cliraop → HomePod）==="
setsid python3 "$RELAY_SCRIPT" "$SERVER_IP" --target "$HOMEPOD_IP" > /tmp/relay.log 2>&1 < /dev/null &
sleep 5
pgrep -f "cliraop" > /dev/null && echo "  relay+cliraop OK" || echo "  relay 启动中（检查日志）"

echo ""
echo "=== 完成。日志: /tmp/relay.log, /tmp/dacp-bcast.log ==="
echo "播放音频到 $BRIDGE_SINK 即可输出到 HomePod"
