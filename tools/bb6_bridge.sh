#!/usr/bin/env bash
# bobanana6 的桥入口 wrapper —— 给 contactor 的 `subprocess_cli` 后端调。
#
# 用法（由 contactor 调）：bb6_bridge.sh "<prompt 里含 task.yaml 路径>"
#
# 为什么单独一个文件：
#   ① contactor 的 agent `command` 是按 argv 数组执行的。实测 **不要**套
#      `bash -lc "… \"$@\""` —— `bash -c` / `bash -lc` 都会把位置参数吃掉（`argc=0`），
#      prompt 传不进来，表现是"脚本跑了但说没找到 task.yaml"。直接 exec 本脚本即可。
#   ② 模型端点/key 需要从这里注入（见下）。
#
# ★ 环境从哪来：contactor 的 `subprocess_cli` **没有 `env` 字段**，而
#   `wsl.exe` 也**不会**把 Windows 侧的环境变量带进 WSL（实测：WSL 里读到空）。
#   所以端点配置走一个**文件**，由 WSL 侧自己读 —— 这也是凭据该待的地方
#   （不进命令行、不进日志、不进桥的配置文件）。
set -u

#: 端点与 key 的来源。格式与 .env 一致（KEY=VALUE 每行一条）。
#: 没有这个文件也能跑 —— 那就得靠调用方自己把变量放进 WSL 的环境里。
BB6_BRIDGE_ENV="${BB6_BRIDGE_ENV:-$HOME/.bb6_bridge.env}"
if [ -f "$BB6_BRIDGE_ENV" ]; then
    # 只 export 定义，不执行任意代码
    set -a
    # shellcheck disable=SC1090
    . "$BB6_BRIDGE_ENV"
    set +a
fi

cd /mnt/c/Users/45140/OneDrive/Desktop/项目/bobanana6 || exit 1
exec .venv/bin/python tools/bb6_agent_entry.py "$@"
