# Qwen3-TTS Web 与 SpeakVideo Windows Agent

这个仓库提供两部分能力：

1. 跨平台的 Qwen3-TTS 本地 Web 服务和音色工作台。
2. 面向 SpeakVideo 的 Windows GPU Worker。Worker 通过出站 WSS 接收远程任务，并使用本机 NVIDIA GPU 完成推理。

本仓库负责 Qwen 推理、Windows 音色库、任务队列和 Worker 协议。

## 工作模式

### Mac 本机运行

Mac 上直接启动 Qwen3-TTS Web 服务，浏览器访问本机 Web 工作台。Apple Silicon 使用 MPS；模型和音色都保存在 Mac 本机。

## 快速开始

### 环境要求

- Python 3.11，且版本小于 3.12。
- macOS Apple Silicon：使用 MPS，建议 24 GiB 或以上统一内存。
- Windows / Linux NVIDIA：安装可用的 CUDA 驱动；`device = "auto"` 会自动选择设备，也可以设置为 `cuda:0`。
- 模型不会提交到 Git。首次安装和下载需要网络以及足够的磁盘空间。

### Mac、Linux 或 Windows 本地服务

macOS / Linux：

```bash
bash scripts/bootstrap.sh setup
bash scripts/bootstrap.sh serve --host 127.0.0.1
```

Windows PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 setup
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 serve --host 127.0.0.1
```

启动后打开 <http://127.0.0.1:8001>。首次使用可以执行 `doctor` 检查依赖，使用 `download` 下载默认模型：

```bash
bash scripts/bootstrap.sh doctor
bash scripts/bootstrap.sh download
```

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 doctor
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 download
```

### Windows GPU Worker

先复制配置模板并填写 Worker 信息：

```powershell
Copy-Item .\config.example.toml .\config.toml
notepad .\config.toml
```

至少需要配置 `[tts_agent]` 中的：

```toml
[tts_agent]
gateway_url = "wss://your-worker-endpoint.example/v1/worker/ws"
token = "仅填入本机，不要提交到 Git"
worker_id = "WIN-GPU-01"
qwen_url = "http://127.0.0.1:8001"
```

初始化依赖并注册 Windows 计划任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 setup
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\speakvideo\install.ps1 -Start
```

日常启停：

```powershell
.\scripts\speakvideo\start.ps1
.\scripts\speakvideo\stop.ps1
```

只操作某一部分服务：

```powershell
.\scripts\speakvideo\start.ps1 -Mode qwen
.\scripts\speakvideo\start.ps1 -Mode agent
.\scripts\speakvideo\stop.ps1 -Mode agent
```

前台调试可以在 Qwen 服务已经运行后执行：

```powershell
.\scripts\speakvideo\run.ps1 -Mode agent
```

脚本会避免重复启动健康的 Qwen 进程，Agent 会将远程 WSS 服务主机加入 `NO_PROXY`，并把日志写入 `.speakvideo/logs/`。Windows Agent 更新代码后需要重启 Agent 才会加载新接口。

## Worker 接口发现与调用

所有 Worker RPC 都通过现有 Worker WebSocket 连接传输，并保留同一个 `request_id`。不需要开放 Windows 的 8001 端口。

查询动态接口文档：

```json
{
  "type": "worker.capabilities",
  "request_id": "cap-unique-id"
}
```

轻量健康检查：

```json
{
  "type": "worker.health",
  "request_id": "health-unique-id",
  "include_docs": false
}
```

当前 Worker RPC 包括：

| RPC | 用途 |
| --- | --- |
| `worker.capabilities` | 返回版本化、无 Token 的机器可读接口文档 |
| `worker.health` | 返回 Qwen 就绪状态、设备和能力 |
| `worker.metrics` | 返回 GPU、内存、队列和近期事件指标 |
| `job.query` | 查询持久化任务状态，可选返回缓存音频 |
| `voice.list` / `voice.download` | 查询或下载 Windows PT 音色 |
| `voice.create` / `voice.design` | 创建音色或生成设计音色 |
| `voice.delete` | 默认软删除；`permanent: true` 才永久删除 |
| `voice.manage` | 音色导入、编辑、删除、引用音频、回收站和恢复 |

### 音色删除语义

Mac 当前的永久删除请求如下：

```json
{
  "type": "voice.manage",
  "request_id": "voice-delete-unique-id",
  "payload": {
    "action": "delete_permanent",
    "role": "narrator",
    "emotion": "calm",
    "sha256": "当前 PT 文件的 64 位 SHA-256"
  }
}
```

成功响应包含：

```json
{
  "type": "rpc.result",
  "request_id": "voice-delete-unique-id",
  "result": {
    "deleted": true,
    "permanent": true,
    "remote_status": "deleted"
  }
}
```

`voice.manage` 的 `delete` 和不带 `permanent` 的 `voice.delete` 都是软删除，会移动到 `.speakvideo/voice-trash/`；`delete_permanent` 会校验 SHA-256 后删除 PT、同名 JSON 元数据和 WAV 参考音频，不创建回收站记录。SHA 不匹配时请求会失败，以防止 Mac 的旧库存误删后来替换的音色。

## 本地 HTTP API

本地 Qwen 服务启动后提供：

- `/`：语音工作台。
- `/maker`：参考音频和克隆音色管理。
- `/docs`：FastAPI 交互式 API 文档。
- `/api/health`：模型、设备和就绪状态。

Worker 只通过 `qwen_url` 访问这些内部接口。默认 `qwen_url` 是 `http://127.0.0.1:8001`，建议保持回环地址，不要为了让 Mac 直连而暴露 Windows HTTP 服务。

## 数据与目录

| 路径 | 内容 | 是否提交 |
| --- | --- | --- |
| `models/` | Qwen 模型文件 | 否 |
| `pt/<role>/<role>_<emotion>.pt` | Windows 或本地的 PT 音色 | 否 |
| `output/` | WAV 和本地音色数据库 | 否 |
| `.speakvideo/jobs.sqlite3` | Worker 任务状态 | 否 |
| `.speakvideo/voice-trash/` | 软删除音色回收站 | 否 |
| `.speakvideo/logs/` | Windows 服务日志 | 否 |
| `config.toml` | 本机配置和 Worker Token | 否 |

## GPU 和模型说明

- Windows NVIDIA 使用 PyTorch CUDA；默认模型是 Qwen3-TTS Base 1.7B，较小的 Base 0.6B 可降低显存压力。
- Windows 建议 1.7B 至少准备约 6 GiB 显存，0.6B 至少准备约 4 GiB；实际占用取决于 PyTorch、音频长度和并发设置。
- Mac Apple Silicon 使用 MPS。项目不承诺 Intel Mac、AMD GPU 或 MLX 推理路径。
- Worker 队列默认串行，因为单个模型实例和单块 GPU 共享推理锁。
- 生成 WAV 通过 WebSocket 内存中的 base64 返回，Worker 不把生成中间音频写入磁盘。

## 安全边界

- 不要提交 `config.toml`、Worker Token、模型、PT、WAV 或日志。
- 远程服务必须使用 WSS 和 Worker 鉴权；生产环境不要使用匿名 Agent。
- 本地服务默认可以配置为 `127.0.0.1`。只有在明确需要局域网访问且有额外鉴权时才监听 `0.0.0.0`。
- PT 文件可能包含可执行的 PyTorch 序列化内容，只加载可信来源的 PT 文件。

## 开发与测试

安装服务依赖后，在仓库根目录运行：

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

核心 Windows Agent 文件位于 `scripts/speakvideo/`：

```text
speakvideo_agent.py   Worker WSS、RPC 和任务队列
voice_library.py      PT 音色导入、编辑、软删除和永久删除
interface_docs.py     Mac 可查询的动态接口文档
base_agent.py         协议基础类和 Qwen 客户端
start.ps1 / stop.ps1  Windows 启停脚本
```

## 许可证

本项目使用 [MIT License](LICENSE)。Qwen3-TTS、PyTorch、ModelScope、Hugging Face、FastAPI 等上游组件遵循各自许可证。
