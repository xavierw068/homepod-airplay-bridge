#!/usr/bin/env bash
# homepod-audio.sh — 在 HomePod 与本机喇叭之间切换默认输出,并调整音量
#
# 原理:默认 sink 指向 homepod-bridge-sink 时,本机任何播放器/浏览器的声音
# 都会被桥接栈(需已通过 start.sh 启动)送到两台 HomePod。
#
# 用法:
#   homepod-audio.sh homepod     切到 HomePod(默认输出)
#   homepod-audio.sh speaker     切回本机喇叭
#   homepod-audio.sh toggle      在两者之间切换
#   homepod-audio.sh status      显示当前输出、音量、HomePod 连接
#   homepod-audio.sh vol up      音量 +5%
#   homepod-audio.sh vol down    音量 -5%
#   homepod-audio.sh vol 70      音量设为 70%

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 加载本地配置 (load local config)
if [ ! -f "$SCRIPT_DIR/config.env" ]; then
    echo "缺少 config.env: cp config.env.example config.env 并填写" >&2
    exit 1
fi
. "$SCRIPT_DIR/config.env"

HPSINK="${BRIDGE_SINK:-homepod-bridge-sink}"

# 自动找本机喇叭:非 bridge 的第一个 alsa 输出
LOCALSINK=$(pactl list sinks short 2>/dev/null \
            | grep -v "$HPSINK" \
            | grep -E "alsa_output" \
            | head -1 | awk '{print $2}')

# fallback:config.env 里的 LOCAL_SINK(可选)
if [ -z "$LOCALSINK" ] && [ -n "${LOCAL_SINK:-}" ]; then
    LOCALSINK="$LOCAL_SINK"
fi
if [ -z "$LOCALSINK" ]; then
    echo "⚠ 找不到本机喇叭 sink,请在 config.env 里设置 LOCAL_SINK" >&2
fi

usage() {
    sed -n '2,11p' "$0" | sed 's/^# //; s/^#$//'
}

cur_sink() {
    pactl get-default-sink 2>/dev/null
}

human_sink() {
    local s
    s=$(cur_sink)
    if [ "$s" = "$HPSINK" ]; then echo "HomePod"; else echo "本机喇叭($s)"; fi
}

show_status() {
    local s vol mute
    s=$(cur_sink)
    vol=$(pactl get-sink-volume "$s" | grep -oE "[0-9]+%" | head -1)
    mute=$(pactl get-sink-mute "$s" | grep -oE "(yes|no)" | head -1)
    echo "当前输出: $(human_sink)  音量: $vol  静音: $mute"
    if [ "$s" = "$HPSINK" ]; then
        n=$(ss -tn 2>/dev/null | grep -cE "ESTAB .*:7000")
        echo "HomePod 连接: ${n:-0}/2"
        if [ "${n:-0}" -lt 2 ]; then
            echo "  ⚠ 只有 $n 台连着——桥接栈在跑吗?(bash start.sh)"
        fi
    fi
}

set_vol() {
    local s="$1" cmd="$2"
    case "$cmd" in
        up)   pactl set-sink-volume "$s" +5% ;;
        down) pactl set-sink-volume "$s" -5% ;;
        *)
            # pactl 的裸数字被当成 /65536 的线性值(如 55 ≈ 0%),
            # 所以纯数字必须补上 %
            case "$cmd" in
                *%*) pactl set-sink-volume "$s" "$cmd" ;;
                *)   pactl set-sink-volume "$s" "${cmd}%" ;;
            esac
            ;;
    esac
    echo "→ $(human_sink) 音量: $(pactl get-sink-volume "$s" | grep -oE '[0-9]+%' | head -1)"
}

switch_to() {
    pactl set-default-sink "$1"
    # 已运行的播放器不自动跟随,尽量把它们也挪过去(忽略失败)
    pactl list sink-inputs short 2>/dev/null | awk '{print $1}' | while read -r i; do
        pactl move-sink-input "$i" "$1" 2>/dev/null
    done
    echo "已切换到: $(human_sink)"
    show_status
}

cmd="${1:-status}"
case "$cmd" in
    homepod)  switch_to "$HPSINK" ;;
    speaker)  switch_to "$LOCALSINK" ;;
    toggle)
        if [ "$(cur_sink)" = "$HPSINK" ]; then switch_to "$LOCALSINK"; else switch_to "$HPSINK"; fi
        ;;
    status)   show_status ;;
    vol)
        [ -n "$2" ] || { usage; exit 1; }
        set_vol "$(cur_sink)" "$2"
        ;;
    *) usage ;;
esac
