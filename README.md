# bonsai-for-DSH

把 [Ternary Bonsai 2.5 27B](https://huggingface.co/prism-ml/Ternary-Bonsai-2.5-27B-GGUF) 跑在 Apple Silicon 上，
接进 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的 `Local Model` provider ——
**按需加载、空闲自动卸载、可选开机自启（只启代理，不预加载权重）**。

> 27B 的三进制权重，1.76 bit/weight，7.0 GB 一个文件，Metal 上 20+ tok/s，原生工具调用。
> 官方 llama.cpp 读不了 `PQ2_0` 打包，所以运行时用 PrismML 的 fork。

```
DSH ──► bonsai 代理 :8123 ──(首次请求才拉起)──► llama-server :8124 (Metal)
         │                                        │
         └─ 空闲 900s 后关掉 llama-server ─────────┘   ← 内存还回去
```

## 快速开始

```bash
git clone https://github.com/duhaoze2007/bonsai-for-DSH.git
cd bonsai-for-DSH
./installer/install.sh --autostart      # 下载 7.0 GB 权重 + 运行时，配置登录自启
```

然后把这个文件的配置追加到 `~/.dsh/profiles/<profile>/cordis.patch.yml` 末尾：

```bash
cat dsh/bonsai-provider.patch.yml
```

**重启 DSH 应用**（文件型配置不是热更新），模型选择器里就会出现
`Local Model / Bonsai 2.5 27B (local Metal)`。

## 命令

| 命令 | 作用 |
| --- | --- |
| `bonsai start` / `stop` / `restart` | 起/停代理；`stop` 会连模型一起卸载 |
| `bonsai status` | 代理 + 模型状态、内存、下次自动卸载倒计时 |
| `bonsai load` / `unload` | 手动预热 / 立即卸载（`unload` 立刻还回 ~10 GB） |
| `bonsai chat "问题"` | 冒烟测试一次对话（会触发按需加载） |
| `bonsai tool-test` | 验证原生工具调用 |
| `bonsai logs [n]` / `proxy-logs [n]` | 看后端 / 代理日志 |
| `bonsai doctor` | 自检：文件、运行时、端口、自启状态 |
| `bonsai thinking …` | 思考模式：力度 / 预算上限 / 完全关闭 |
| `bonsai autostart on\|off\|status` | 登录自启开关（LaunchAgent，只跑代理） |

`bonsai` 会被装到 `~/.local/bin`，脚本本身按真实路径定位安装目录，仓库放哪都行。

## 三种「加载/卸载」粒度

1. **真的按需（默认）**：DSH 发第一条推理请求时代理才拉起 llama-server
   （`/v1/models`、`/health` 不触发加载，所以模型列表秒回）；
   最后一条请求后空闲 `idle_unload_seconds`（默认 900s）自动卸载。
2. **手动**：`bonsai load` 预热、`bonsai unload` 立即释放内存。
3. **彻底停**：`bonsai stop`，代理和模型都退出（自启已开的话下次登录会再起来）。

## 思考模式：想太久 / 绕圈子怎么办

这批三进制模型是「默认思考」的（Qwen3.8 底座），思考 token 也走解码预算。所以有两个坑：

- **想太久**：思考吃掉全部 `max_tokens`，正文一个字都没出来 —— 在 agent 循环里看起来就像卡住。
- **绕圈子**：上一代 Bonsai 2 被抱怨过（[HF discussion #63](https://huggingface.co/prism-ml/Ternary-Bonsai-27B-gguf/discussions/63)），
  官方说 2.5 就是来修这个的；但 2.5 的讨论区仍有「Overthinker」反馈
  （[#6](https://huggingface.co/prism-ml/Ternary-Bonsai-2.5-27B-GGUF/discussions/6)），
  有人在 CPU + Q2_0 上真的绕成了「我保证这是最后一次问你」。

本机实测（M4 Pro，18 次运行，3 档 × 6 个 prompt）：**没有复现重复循环**（30 字符滑窗重复率 0%），
但**复现了想太久**：无上限的 xhigh 问「详细设计电商订单系统表结构」，思考吃掉全部 1500 token，
回答 0 字；同一问题加 `budget 200` 后，思考收到 200 token 就收尾，答出 2782 字完整设计。

所以给了四档：

```bash
bonsai thinking                      # 看当前档位
bonsai thinking xhigh                # 默认，最准
bonsai thinking medium               # 少想一点（实测和 xhigh 差别不稳定）
bonsai thinking budget 1024          # 保留思考但设硬上限，超了强制收尾作答 ← 默认
bonsai thinking budget none          # 取消上限
bonsai thinking off                  # 完全不思考
```

`budget` 走 llama.cpp 的 `--reasoning-budget`，`off` 走 `--reasoning off`（模板直接给空 think 块）。
改完只需要卸载后端，下一条请求自动按新档位重载：

| 档位 | 适合 | 实测（同一批 prompt） |
| --- | --- | --- |
| `off` | 闲聊、短问答、要最快 | 短问题 2.6s vs 6.0s；脚本类 12.8s vs 38.8s；复杂推理答得更长也更浅 |
| `budget 1024`（默认） | 日常 agent / 工具调用 | 正常问题思考 400–500 token，跑飞时兜得住 |
| `budget 200` | 只要快、能接受浅一点 | 强制收尾，仍能给出完整答案 |
| `xhigh` 无上限 | 硬推理、离线批处理 | 质量最好，但可能一个答案都不给 |

> 权衡：官方 benchmark 全部是思考模式测的，关掉思考会掉质量，复杂任务建议留着、只设预算。

## 实测（MacBook Pro M4 Pro / 24 GB）

| 指标 | 数值 |
| --- | --- |
| 模型加载 | 15s（首次读盘）、4s（文件已在页缓存） |
| 常驻内存 | 10.0–10.4 GB（含 32K 上下文 KV） |
| 生成速度 | 20–21 tok/s（MTP 草稿接受率 0.82–1.0） |
| 预填充 | 90–130 tok/s；16K token 的 agent 提示约 3 分钟 |
| 工具调用 | 原生 `tool_calls`，流式增量、多轮循环都正常 |

在 DSH 里跑通的一轮真实 agent：让它「用 bash 执行 `echo bonsai-dsh-ok` 并只回复输出」，
模型调 bash 工具、拿到结果、只回 `bonsai-dsh-ok`，用时 3 分 41 秒（其中 3 分钟是首轮 16K 预填充）。

> 提示：DSH 的 agent 系统提示 + 工具定义约 16K token，首轮慢是预填充，不是模型慢。
> 同一会话后续轮次靠 llama.cpp 前缀缓存增量预填充。

## 配置

`config.json`（`installer/install.sh` 从 `config.example.json` 生成）：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `proxy_port` / `backend_port` | 8123 / 8124 | 代理对外的端口 / 后端仅本机 |
| `context_size` | 32768 | 上下文；降到 16384 省约 1 GB，但 agent 提示就占掉大半 |
| `idle_unload_seconds` | 900 | 空闲多久卸载；`0` = 永不自动卸载 |
| `thinking` | `{"mode":"xhigh","budget":1024}` | 思考档位；`mode` = `xhigh`/`medium`/`low`/`off`，`budget` = 思考 token 硬上限（`null` = 不限） |
| `server_args` | 见文件 | 透传给 llama-server 的参数（Metal 全量 offload + MTP 草稿头 + 采样默认值） |

`server_args` 和 `context_size` 改完要 `bonsai restart`；`thinking` 用 `bonsai thinking …` 改，
只会卸载后端、下一条请求自动重载。

- **MTP 草稿头**：`--spec-type draft-mtp --spec-draft-n-min 0 --spec-draft-n-max 1 --spec-draft-p-min 0.6`
  是官方 Metal preset 在 M4 Pro 上的设置。去掉这三个参数就是纯解码（约 22 tok/s，预填充略快）。
- **采样**：模型卡推荐 thinking 模式 `temp=1.0 / top_p=0.95 / top_k=20 / min_p=0`。
  采样前三项写在 `server_args` 里当服务端默认值，`temperature` 由 DSH 侧下发。

## 换成原版 llama.cpp + Q2_0

不想用 fork 二进制时，可以换官方的 `Q2_0` 打包 + Homebrew 的 llama.cpp（需 ≥ build b11556）：

```bash
cd models
aria2c -x16 -s16 -o Ternary-Bonsai-2.5-27B-Q2_0.gguf \
  https://huggingface.co/prism-ml/Ternary-Bonsai-2.5-27B-GGUF/resolve/main/Ternary-Bonsai-2.5-27B-Q2_0.gguf
```

然后改 `config.json` 的 `model_path`、把 `runtime_dir` 指向 `/opt/homebrew/bin`
（或把 `SERVER_BIN` 换成绝对路径），并删掉 `server_args` 里的 `--spec-type` 三项。
Q2_0 慢一些（官方同卡 109 vs 150 tok/s），好处是运行时可以直接 `brew upgrade`。

## 参数与兼容性

| 组件 | 版本 |
| --- | --- |
| 权重 | `Ternary-Bonsai-2.5-27B-PQ2_0-mtp.gguf`，sha256 `ededc9fc…aa6c23`（与官方 preset 一致） |
| 运行时 | PrismML llama.cpp fork `prism-b12232-f702476`（macOS arm64） |
| 代理 | Python 3.9+ 标准库，无第三方依赖 |
| DSH | 需要装有 `dsh-local-model` 插件（provider id `local`） |

## 常见问题

**只输出思考、正文是空的 / 回合被截断** —— 思考 token 吃完了预算。
`bonsai thinking budget 1024`（或更小）给它设上限，或者 `bonsai thinking off` 直接关掉。

**模型列表里有，但一发消息就卡住** —— 首次请求在加载权重（15s 左右）+ 预填充，
DSH 那边的流超时给足（`streamIdleTimeoutMs` 至少 600000）。

**内存不够** —— 调小 `context_size`，或把 `idle_unload_seconds` 调小到 300，
让它更快自动卸载。

**自启了但模型占着内存** —— 不会。LaunchAgent 只跑代理（几十 MB），
`bonsai status` 里模型应显示「未加载（按需加载）」。

**飞书/桌面小挂件异常** —— 与本项目无关；跑 `bonsai status` 确认代理端口没被别的进程占用。

**换了机器（Linux/CUDA）** —— `bin/llama-proxy.py` 和 `bin/bonsai` 与平台无关，
只需把 `runtime/` 换成对应平台的 fork 包（`bin-linux-cuda-*`），并用 `PTQ1_0` 或 `PQ2_0` 权重。

## 许可

代码 MIT（见 [LICENSE](LICENSE)）。模型权重是 Apache 2.0，来自
[prism-ml/Ternary-Bonsai-2.5-27B-GGUF](https://huggingface.co/prism-ml/Ternary-Bonsai-2.5-27B-GGUF)；
llama.cpp fork 的许可见 [PrismML-Eng/llama.cpp](https://github.com/PrismML-Eng/llama.cpp)。
本仓库不包含权重和二进制，安装时按需下载。
