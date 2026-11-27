#!/usr/bin/env bash
# install.sh —— 一键把 Ternary Bonsai 2.5 27B 装到本机，并接进 DeepSeek Harness。
#
#   ./installer/install.sh                 # 下载权重 + 运行时，生成配置
#   ./installer/install.sh --autostart     # 顺带配置登录自启（只启代理，模型仍按需加载）
#   ./installer/install.sh --skip-model    # 权重已经有了，只装运行时
#
# 目标环境：macOS Apple Silicon（Metal）。Linux/CUDA 需要换运行时包，见 README。
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_REPO="prism-ml/Ternary-Bonsai-2.5-27B-GGUF"
MODEL_FILE="Ternary-Bonsai-2.5-27B-PQ2_0-mtp.gguf"
MODEL_SHA256="ededc9fc0a8a6a8dd30914d107b2d2ce7674d789f1d05a1a481f240609aa6c23"
MODEL_SIZE="7.0 GB"
RUNTIME_TAG="prism-b12232-f702476"
RUNTIME_ARCH="macos-arm64"

AUTOSTART=0
SKIP_MODEL=0
for arg in "$@"; do
    case "$arg" in
        --autostart) AUTOSTART=1 ;;
        --skip-model) SKIP_MODEL=1 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

say() { printf "\033[36m▸ %s\033[0m\n" "$*"; }
ok() { printf "\033[32m✅ %s\033[0m\n" "$*"; }
warn() { printf "\033[33m! %s\033[0m\n" "$*"; }
die() { printf "\033[31m✖ %s\033[0m\n" "$*" >&2; exit 1; }

# ── 环境检查 ──────────────────────────────────────────────
[ "$(uname -s)" = "Darwin" ] || die "这个脚本只覆盖 macOS；Linux 请参考 README 换运行时包"
[ "$(uname -m)" = "arm64" ] || warn "不是 arm64，Metal 加速可能用不上"

command -v python3 >/dev/null || die "需要 python3（代理和管理器都用它）"

# ── 1. 下载权重 ───────────────────────────────────────────
mkdir -p "$BASE/models" "$BASE/logs"
MODEL_PATH="$BASE/models/$MODEL_FILE"

if [ "$SKIP_MODEL" = "0" ]; then
    if [ -f "$MODEL_PATH" ] && [ "$(shasum -a 256 "$MODEL_PATH" | cut -d' ' -f1)" = "$MODEL_SHA256" ]; then
        ok "权重已存在且校验通过：$MODEL_FILE"
    else
        say "下载权重 $MODEL_FILE（约 $MODEL_SIZE）→ $BASE/models"
        URL="https://huggingface.co/$MODEL_REPO/resolve/main/$MODEL_FILE"
        if command -v aria2c >/dev/null; then
            aria2c -x16 -s16 -k4M --file-allocation=none -c -d "$BASE/models" -o "$MODEL_FILE" "$URL"
        else
            curl -L --fail --retry 3 -C - -o "$MODEL_PATH" "$URL"
        fi
        say "校验 sha256…"
        ACTUAL="$(shasum -a 256 "$MODEL_PATH" | cut -d' ' -f1)"
        [ "$ACTUAL" = "$MODEL_SHA256" ] || die "sha256 不匹配：$ACTUAL（删掉文件重下）"
        ok "权重校验通过"
    fi
else
    warn "跳过权重下载（--skip-model）"
fi

# ── 2. 下载运行时（PrismML fork，上游 llama.cpp 读不了 PQ2_0）──
mkdir -p "$BASE/runtime"
if [ -x "$BASE/runtime/llama-server" ] && "$BASE/runtime/llama-server" --version 2>&1 | grep -q "$RUNTIME_TAG\|$(echo "$RUNTIME_TAG" | sed 's/.*-b\([0-9]*\)-.*/\1/')"; then
    ok "运行时已就绪：$("$BASE/runtime/llama-server" --version 2>&1 | head -1)"
else
    say "下载运行时 llama.cpp fork（$RUNTIME_TAG / $RUNTIME_ARCH）"
    TMP="$(mktemp -d)"
    tar_url="https://github.com/PrismML-Eng/llama.cpp/releases/download/$RUNTIME_TAG/llama-$RUNTIME_TAG-bin-$RUNTIME_ARCH.tar.gz"
    curl -L --fail --retry 3 "$tar_url" -o "$TMP/llama.tar.gz"
    tar -xzf "$TMP/llama.tar.gz" -C "$BASE/runtime" --strip-components=1
    rm -rf "$TMP"
    ok "运行时已解压：$("$BASE/runtime/llama-server" --version 2>&1 | head -1)"
fi

# ── 3. 生成 config.json ───────────────────────────────────
if [ -f "$BASE/config.json" ]; then
    warn "config.json 已存在，保留不动（要重建就删掉它再跑一次）"
else
    python3 - "$BASE" <<'PY'
import json, os, sys
base = sys.argv[1]
cfg = json.load(open(os.path.join(base, "config.example.json")))
text = json.dumps(cfg, ensure_ascii=False, indent=2).replace("<BASE>", base)
open(os.path.join(base, "config.json"), "w").write(text + "\n")
PY
    ok "已生成 config.json"
fi
chmod +x "$BASE/bin/bonsai" "$BASE/bin/llama-proxy.py" "$BASE/bin/spawn.py"

# ── 4. 把 bonsai 挂到 PATH ────────────────────────────────
mkdir -p "$HOME/.local/bin"
ln -sfn "$BASE/bin/bonsai" "$HOME/.local/bin/bonsai"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ok "已链接 ~/.local/bin/bonsai" ;;
    *) warn "已链接 ~/.local/bin/bonsai，但该目录不在 PATH 里，自己加一下" ;;
esac

# ── 5. 起代理（只起代理，不加载模型）──────────────────────
if [ "$AUTOSTART" = "1" ]; then
    "$BASE/bin/bonsai" autostart on
else
    "$BASE/bin/bonsai" start
fi

cat <<EOF

下一步：把下面这段加到 ~/.dsh/profiles/<profile>/cordis.patch.yml 末尾，
然后重启 DeepSeek Harness（文件型配置不是热更新）：

$(sed 's/^/  /' "$BASE/dsh/bonsai-provider.patch.yml")

模型只在第一次推理请求时加载，空闲 $(python3 -c "import json;print(json.load(open('$BASE/config.json'))['idle_unload_seconds'])")s 自动卸载。
EOF
