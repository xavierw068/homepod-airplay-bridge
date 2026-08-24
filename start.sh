#!/bin/bash
# HomePod Bridge 启动脚本 (start script)
# 启动 audio_stream_server + slimproto_server + squeeze2raop → HomePod 立体声对
# 主路径(HTTP-stream path):default entry point.
#
# 音量策略:RAOP 侧音量由 slimproto_server 恒发 $VOLUME_PCT% (~-12dB),
# 响度用 sink 音量微调:pactl set-sink-volume <BRIDGE_SINK> <百分比>
#
# 用法/Usage: bash start.sh(需先 cp config.env.example config.env 并填写)

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 加载本地配置 (load local config)
if [ ! -f "$SCRIPT_DIR/config.env" ]; then
  echo "缺少 config.env: cp config.env.example config.env 并填写" >&2
  exit 1
fi
. "$SCRIPT_DIR/config.env"

# RAOP 配置路径支持绝对路径或相对仓库根
case "$RAOP_CONFIG" in
  /*) : ;;
  *)  RAOP_CONFIG="$SCRIPT_DIR/$RAOP_CONFIG" ;;
esac

VOLUME_PCT="${VOLUME_PCT:-50}"

# 清理旧进程 (cleanup stale processes)
for pid in $(pgrep -f "audio_stream_server" 2>/dev/null); do kill -9 $pid 2>/dev/null; done
for pid in $(pgrep -f "squeeze2raop" 2>/dev/null); do kill -9 $pid 2>/dev/null; done
for pid in $(pgrep -f "slimproto_server" 2>/dev/null); do kill -9 $pid 2>/dev/null; done
sleep 2

cd "$SCRIPT_DIR/src"

# 启动音频流服务器 (audio stream server)
nohup python3 -u audio_stream_server.py --http-port "$HTTP_PORT" --sink "$BRIDGE_SINK" \
    > /tmp/audio-server.log 2>&1 &
AUDIO_PID=$!
echo "audio_stream_server: $AUDIO_PID"

# 启动 slimproto 服务器 (恒发 $VOLUME_PCT% 音量,内部 keepalive 每 4s 重申)
nohup python3 -u slimproto_server.py --port "$SLIMPROTO_PORT" --http-port "$HTTP_PORT" \
    --left-mac "${HOMEPOD_LEFT_MAC:-}" --right-mac "${HOMEPOD_RIGHT_MAC:-}" \
    > /tmp/slimproto-server.log 2>&1 &
SLIM_PID=$!
echo "slimproto_server: $SLIM_PID"

# 启动 squeeze2raop (-Z 无 avahi 模式; DACP 端口区间在防火墙放行范围内)
nohup "$SQUEEZE2RAOP_BIN" -Z -s "$SERVER_IP:$SLIMPROTO_PORT" -a "$DACP_PORT_RANGE" \
    -x "$RAOP_CONFIG" -f /tmp/s2r.log -d all=info -c alac > /dev/null 2>&1 &
S2R_PID=$!
echo "squeeze2raop: $S2R_PID"

# 音量:RAOP 端恒 $VOLUME_PCT%,响度由 sink 音量控制
pactl set-sink-volume "$BRIDGE_SINK" "${VOLUME_PCT}%" 2>/dev/null
echo "sink 音量: $VOLUME_PCT% (pactl set-sink-volume $BRIDGE_SINK <pct> 调整)"

# 自愈:crosslink 占着 UDP 5353 (mDNS),会间歇性抢走 HomePod 的 mDNS 响应,
# 导致 squeeze2raop 启动后漏发现设备(有时 0 台、有时只 1 台)→ 无声/只响一台。
# 这里要求预期台数的设备都出现(默认 = raopbridge.xml 里 enabled 的 <device> 数),
# 不足就自动重启 squeeze2raop(每次重启都是新的 bind/查询,响应分发会重新随机)。
EXPECTED_HOMEPODS="${EXPECTED_HOMEPODS:-$(grep -c '<enabled>1</enabled>' "$RAOP_CONFIG" 2>/dev/null || echo 2)}"
[ "$EXPECTED_HOMEPODS" -ge 1 ] 2>/dev/null || EXPECTED_HOMEPODS=2
DISC_BASELINE=$(grep -c 'AddRaopDevice' /tmp/s2r.log 2>/dev/null || echo 0)
MAX_ATTEMPTS=6
attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
    got=0
    for _ in $(seq 1 8); do
        got=$(grep -c 'AddRaopDevice' /tmp/s2r.log 2>/dev/null || echo 0)
        [ "$((got - DISC_BASELINE))" -ge "$EXPECTED_HOMEPODS" ] && break
        sleep 2
    done
    if [ "$((got - DISC_BASELINE))" -ge "$EXPECTED_HOMEPODS" ]; then
        echo "squeeze2raop: 设备发现成功 $((got - DISC_BASELINE))/$EXPECTED_HOMEPODS (尝试 $attempt/$MAX_ATTEMPTS)"
        break
    fi
    if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
        echo "squeeze2raop: 只发现 $((got - DISC_BASELINE))/$EXPECTED_HOMEPODS,自动重启 (尝试 $attempt/$MAX_ATTEMPTS)..."
        kill -9 "$S2R_PID" 2>/dev/null
        sleep 1
        DISC_BASELINE=$(grep -c 'AddRaopDevice' /tmp/s2r.log 2>/dev/null || echo 0)
        nohup "$SQUEEZE2RAOP_BIN" -Z -s "$SERVER_IP:$SLIMPROTO_PORT" -a "$DACP_PORT_RANGE" \
            -x "$RAOP_CONFIG" -f /tmp/s2r.log -d all=info -c alac > /dev/null 2>&1 &
        S2R_PID=$!
        echo "squeeze2raop: 重启后 PID $S2R_PID"
    else
        echo "squeeze2raop: 连续 $MAX_ATTEMPTS 次仍只发现 $((got - DISC_BASELINE))/$EXPECTED_HOMEPODS,放弃"
    fi
    attempt=$((attempt+1))
done
sleep 3
echo ""
echo "=== 组件状态 (component status) ==="
echo "audio:       $([ -n "$(pgrep -f audio_stream_server)" ] && echo OK || echo FAIL)"
echo "slimproto:   $([ -n "$(pgrep -f slimproto_server)" ] && echo OK || echo FAIL)"
echo "squeeze2raop: $([ -n "$(pgrep -f 'squeeze2raop.* -Z')" ] && echo OK || echo FAIL)"

echo ""
echo "=== HomePod 连接状态 (期望 2 台都 raop connected) ==="
grep -c "raop connected" /tmp/s2r.log 2>/dev/null | xargs -I{} echo "raop connected 次数: {}"
ss -tn 2>/dev/null | grep -c ':7000' | xargs -I{} echo "到 HomePod 的 RTSP 连接: {}"

echo ""
echo "=== s2r 日志末尾 (tail of s2r log) ==="
tail -8 /tmp/s2r.log 2>/dev/null
