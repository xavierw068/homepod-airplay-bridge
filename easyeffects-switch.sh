#!/usr/bin/env bash
# easyeffects-switch.sh — 保存 / 切换 EasyEffects 配置快照
#
# ================================================================
#  用法:
#    easyeffects-switch.sh list                列出所有快照
#    easyeffects-switch.sh save <名字>         把当前配置存为快照
#    easyeffects-switch.sh switch <名字>       切换到指定快照并重启 EasyEffects
#    easyeffects-switch.sh help                显示本帮助 (-h / --help 也可)
#
#  内置快照:
#    jblgo5    JBL Go 5 的原 EQ 配置(小喇叭补偿曲线)
#    homepod   按 HomePod 特性调整的 EQ(低频摊平、去掉中频凹陷、消高音齿音)
#
#  示例:
#    easyeffects-switch.sh list
#    easyeffects-switch.sh switch homepod      切换到 HomePod 曲线
#    easyeffects-switch.sh switch jblgo5       切回 JBL 曲线
#    easyeffects-switch.sh save 我的自定义     把当前 GUI 状态存成快照
#
#  说明:
#    · 快照保存在 ~/.config/easyeffects.snapshots/<名字>/,
#      每个快照是完整的 EasyEffects 配置树(可直接用 cp 手动备份)
#    · switch 会重启 EasyEffects,音频会短暂中断几秒
#    · switch 会丢弃当前未保存的临时调整——
#      先在 EasyEffects GUI 里调好,再执行 save 保存
#    · 切换后脚本会自动把正在播放的应用挪回 easyeffects_sink
#    · EasyEffects 运行中直接改它的配置文件会在退出时被覆盖,
#      所以本脚本一律先停进程再换文件(这也是 switch 会重启的原因)
# ================================================================

SNAPDIR="$HOME/.config/easyeffects.snapshots"
CFGDIR="$HOME/.config/easyeffects"

usage() {
    # 打印文件头部的 # 注释块(直到第一个非注释行为止)
    awk 'NR==1 {next}
         /^#/ {sub(/^# /, ""); sub(/^#/, ""); print; next}
         {exit}' "$0"
}

list_snapshots() {
    echo "现有快照:"
    if [ -d "$SNAPDIR" ]; then
        find "$SNAPDIR" -maxdepth 1 -mindepth 1 -type d -printf '  %f\n' | sort
    fi
}

save_snapshot() {
    local name="$1"
    [ -n "$name" ] || { echo "需要一个快照名字,例如: $0 save 我的eq"; return 1; }
    mkdir -p "$SNAPDIR"
    rm -rf "$SNAPDIR/$name"
    cp -r "$CFGDIR" "$SNAPDIR/$name"
    echo "已把当前配置存为快照: $name"
    echo "  → $SNAPDIR/$name"
}

switch_to() {
    local name="$1"
    [ -d "$SNAPDIR/$name" ] || { echo "没有快照 \"$name\""; list_snapshots; return 1; }

    # 停 EasyEffects (精确进程名匹配,避免误杀)
    pkill -x easyeffects 2>/dev/null
    sleep 1

    # 换配置
    rm -rf "$CFGDIR"
    cp -r "$SNAPDIR/$name" "$CFGDIR"

    # 重启(独立进程组,不随本 shell 退出)
    setsid nohup easyeffects >/dev/null 2>&1 &
    echo "已切换到快照: $name"
    sleep 2

    if pgrep -x easyeffects >/dev/null; then
        echo "EasyEffects 已重启"
        # 把已运行的播放器挪回 easyeffects sink(如果它又新建了 sink 节点)
        local ee_sink
        ee_sink=$(pactl list sinks short 2>/dev/null | grep -i easyeffects | head -1 | awk '{print $2}')
        if [ -n "$ee_sink" ]; then
            pactl list sink-inputs short 2>/dev/null | awk '{print $1}' | while read -r i; do
                pactl move-sink-input "$i" "$ee_sink" 2>/dev/null
            done
        fi
    else
        echo "⚠ EasyEffects 启动失败——检查 DISPLAY 环境或手动运行 easyeffects"
    fi
}

cmd="${1:-list}"
case "$cmd" in
    list)      list_snapshots ;;
    save)      save_snapshot "$2" ;;
    switch)    switch_to "$2" ;;
    help|-h|--help) usage ;;
    *)         usage ;;
esac
