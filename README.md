# Qwen3-TTS Voice Clone API

本地语音克隆服务，按「角色 / 情感」管理音色，提供 Web 台词编辑器、音色打包器、HTTP API 与中文 TUI。支持 Windows / Linux NVIDIA CUDA 和 macOS Apple Silicon MPS。

## 跨平台快捷入口

克隆项目后，在项目目录执行一行命令进入 TUI：

| 平台 | 命令 |
| --- | --- |
| macOS Apple Silicon | `bash scripts/bootstrap.sh` |
| Linux NVIDIA | `bash scripts/bootstrap.sh` |
| Windows PowerShell | `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1` |

首次运行会先询问是否安装轻量管理工具及 Python 3.11。进入 TUI 后选择「安装 / 修复」，确认后安装推理依赖并自动下载当前 Base 模型；然后选择「启动服务」。不需要手动激活虚拟环境，也不要求预先安装 Python。

引导程序不自动安装 Homebrew / Scoop 或修改全局镜像配置。安装系统音频工具需要单独勾选并确认。原生 arm64 Python 用于 Apple Silicon；Intel Mac、纯 CPU、AMD GPU 和 MLX 不在本版本支持范围。

### 无界面命令

macOS / Linux：

```bash
bash scripts/bootstrap.sh doctor
bash scripts/bootstrap.sh setup
bash scripts/bootstrap.sh download
bash scripts/bootstrap.sh serve
```

Windows PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 doctor
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 setup
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 download
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 serve
```

以上入口支持相同参数：`--yes` 用于无人值守确认安装/下载，`--offline` 禁止下载，`--model 0.6B` 切换轻量 Base，`--device mps` 或 `--device cuda:0` 指定设备，`--source official` 使用官方源。管理工具安装完成后也可使用其中的 `qwen3-tts-web` 命令。

`doctor` 仅检查环境并返回状态码，不修复、不下载模型。首次使用引导脚本时，仍需先安装管理工具；已有工具的后续检查不联网。非交互终端应明确指定子命令。

## 启动后

| 地址 | 页面 |
| --- | --- |
| `http://localhost:8001/` | 角色台词编辑器 |
| `http://localhost:8001/maker` | 音色打包器 |
| `http://localhost:8001/docs` | API 文档 |
| `http://localhost:8001/api/health` | 模型就绪状态 |

**安全提示：** 为兼容旧版本，默认监听 `0.0.0.0:8001`，无身份认证。仅本机使用请加 `--host 127.0.0.1`。不要将服务直接暴露到公网；只加载可信来源的 `.pt` 文件，PyTorch pickle 文件可以执行代码。

## 环境与模型

- Python 3.11；引导程序优先复用可用解释器，否则经确认通过 uv 获取。
- NVIDIA：默认 1.7B 至少 6 GiB 显存，0.6B 至少 4 GiB；安装前检查选定 GPU，安装后实际执行设备计算自检。当前固定分支不覆盖所有新旧显卡，详细范围见部署文档。
- Apple Silicon：使用 MPS、`float32` 与 `sdpa`；建议 24 GiB 统一内存，并留出足够可用内存。内存不足时提示切换 0.6B，不静默切换模型或 CPU。
- 首次部署建议至少 10 GiB 可用磁盘；Base 1.7B 完整模型约 4.6 GB，下载速度取决于网络。
- 默认只下载 Base 完整快照，包含其内嵌 `speech_tokenizer/`。其他模型可在 TUI「模型」页按需下载，下载不代表新增对应 Web 推理功能。
- 国内源优先并提供官方回退；完整模型存在时直接复用。模型加载在本地离线完成，不会因为缺失文件悄悄访问其他仓库。
- 不默认安装 FlashAttention；环境不支持指定设备时明确报错，不自动回退 CPU。

## 项目结构

```text
.
├── pyproject.toml             # 包元数据、依赖与 CLI
├── config.example.toml        # 项目级配置示例
├── main.py                    # 旧 uvicorn main:app 兼容入口
├── src/qwen3_tts_web/         # 服务、运行时、管理工具、TUI
│   ├── constraints/           # macOS / CUDA 固定依赖分支
│   └── web/                   # 随包发布的原有 Web 页面
├── scripts/                   # 跨平台引导与启动脚本
├── tests/                     # 无硬件回归与可选真机测试
├── docs/                      # 部署、API、开发文档
├── .github/workflows/         # Windows / macOS / Linux CI
├── models/                    # 模型，不提交
├── pt/                        # 原有角色 / 情感音色，不提交
├── uploads/                   # 上传音频，不提交
├── references/                # 参考音频，不提交
└── output/                    # 合成结果，不提交
```

管理工具使用 `.venv-tools/`，推理服务使用 `.venv/`；TUI 日志保存在 `.logs/manager.log`，自动轮转。旧 `models/`、`pt/` 等目录不迁移、不删除；旧 `outputs/` 保留，新输出统一到 `output/`。

## 文档

- [部署、配置与故障排查](docs/deployment.md)
- [API 与音色格式](docs/api.md)
- [开发、测试与真机验收](docs/development.md)
- [本次平台验收记录](docs/validation.md)
- [贡献指南](CONTRIBUTING.md)

## 许可与致谢

本项目使用 MIT 许可证，见 [LICENSE](LICENSE)。Qwen3-TTS 上游代码及模型使用各自声明的许可证，分发时保留上游署名与许可文件。感谢 Qwen3-TTS、PyTorch、ModelScope、FastAPI 与 Textual 社区。
