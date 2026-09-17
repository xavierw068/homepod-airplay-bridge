#!/bin/bash
# HomePod Bridge 停止脚本 (stop script)
# 杀掉主链路 (audio_stream_server + slimproto_server + squeeze2raop)
# 以及 relay 链路 (dacp_broadcast / relay / cliraop / parec)。
#
# 注意:
#  - 不会动 crosslink(剪贴板/KVM 工具),它是用户自己决定保留的。
#  - 不会动 avahi;若之前 start-full.sh 停了它、现在想恢复:
#        sudo systemctl start avahi-daemon
#  - 按进程名匹配,与 start.sh 的清理逻辑一致。

set -u

echo "=== 停止 HomePod Bridge 服务 ==="

# 主链路 (slimproto/HTTP 路径)
for name in audio_stream_server slimproto_server squeeze2raop; do
    pids=$(pgrep -f "$name" 2>/dev/null)
    if [ -n "$pids" ]; then
        # shellcheck disable=SC2086
        kill -9 $pids 2>/dev/null
        echo "已停止: $name (PID $pids)"
    else
        echo "未运行: $name"
    fi
done

# relay/DACP 链路(如果跑过 start-full.sh 或 relay 脚本)
for name in dacp_broadcast relay_parec relay.py cliraop parec; do
    pids=$(pgrep -f "$name" 2>/dev/null)
    if [ -n "$pids" ]; then
        # shellcheck disable=SC2086
        kill -9 $pids 2>/dev/null
        echo "已停止: $name (PID $pids)"
    else
        echo "未运行: $name"
    fi
done

sleep 1

echo ""
echo "=== 剩余相关进程 (应为空) ==="
left=$(ps aux | grep -E '[s]queeze2raop|[s]limproto_server|[a]udio_stream_server|[d]acp_broadcast|[r]elay_parec|[r]elay\.py|[c]liraop|[p]arec' | grep -v grep)
if [ -n "$left" ]; then
    echo "$left"
    echo "⚠ 仍有残留进程,可手动 kill"
else
    echo "全部已停止 ✓"
fi

echo ""
echo "=== 相关端口 (应为空; crosslink 占的 5353 除外) ==="
ports=$(ss -tlnp 2>/dev/null | grep -E ':3483|:9000|:9090|:40[0-1][0-9][0-9]' | grep -v 'crosslink')
if [ -n "$ports" ]; then
    echo "$ports"
    echo "⚠ 仍有端口占用"
else
    echo "端口已释放 ✓"
fi
