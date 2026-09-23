# Qwen3-TTS Voice Clone API

基于 [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) 的本地语音克隆服务，按「角色 / 情感」组织音色文件（pt），提供 HTTP API 与两个 Web 界面，用于音色打包和角色台词批量合成。

- **音色克隆**：上传参考音频，一键打包成 pt 音色文件，按角色 / 情感归档
- **语音合成**：通过 `role + emotion` 或直接指定 pt 文件生成克隆语音
- **音色打包器**：可视化创建、管理音色
- **角色台词编辑器**：按角色组织台词，批量生成对白
- **一键部署**：自动检测 GPU、安装 CUDA 版 PyTorch、下载模型

---

## 目录

- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [目录结构](#目录结构)
- [音色文件（pt）约定](#音色文件pt约定)
- [Web 界面](#web-界面)
- [API 参考](#api-参考)
  - [GET /api/pt/list](#get-apiptlist)
  - [POST /api/pt/create](#post-apiptcreate)
  - [GET /api/pt/inspect](#get-apiptinspect)
  - [POST /api/tts](#post-apitts)
  - [POST /api/pt/upload](#post-apiptupload)
  - [GET /api/pt/download](#get-apiptdownload)
- [模型与精度](#模型与精度)
- [部署脚本说明](#部署脚本说明)
- [依赖与许可](#依赖与许可)

---

## 环境要求

- **系统**：Windows 10 / 11
- **GPU**：NVIDIA，显存 **>= 6 GB**（推荐 8 GB+），计算能力 **>= 6.1**
  - 低于要求时部署脚本会中断并给出提示
  - 显存不足可考虑 `Qwen3-TTS-12Hz-0.6B` 轻量版（约需 3.5 ~ 4 GB）
- **Python**：3.11（部署脚本可通过 [scoop](https://scoop.sh) 自动安装）
- **可选**：`flash-attn`（提升推理速度，脚本会自动尝试安装预编译 wheel）

不满足 GPU 要求时脚本会直接退出，**不支持纯 CPU 运行**。

---

## 快速开始

### 1. 部署环境

在项目根目录打开 PowerShell：

```powershell
.\deploy.ps1
```

脚本会依次完成：

1. 检测 NVIDIA GPU，根据计算能力选择 CUDA 分支
2. 检查 / 安装 Python 3.11
3. 创建 `.venv` 并写入国内 pip 镜像
4. 安装对应 CUDA 版本的 PyTorch
5. 通过 `uv` 并行安装项目依赖（锁定版本）
6. 校验关键包版本，必要时强制修复
7. 可选安装 `flash-attn`
8. 通过 ModelScope 下载模型到 `models/`

### 2. 启动服务

```powershell
.\run.cmd
```

也可以直接：

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8001
```

### 3. 打开界面

| 地址 | 说明 |
|---|---|
| http://localhost:8001/docs | Swagger API 文档 |
| http://localhost:8001/ | 角色台词编辑器 |
| http://localhost:8001/maker | 音色打包器 |

---

## 目录结构

```
.
├── deploy.ps1            # 一键部署脚本
├── run.cmd               # 启动脚本
├── main.py               # FastAPI 服务入口
├── qwen3tts_common.py    # 模型加载与公共工具
├── web/
│   ├── index.html        # 角色台词编辑器
│   └── maker.html        # 音色打包器
├── models/               # 模型目录（不提交）
├── pt/                   # 音色文件目录（不提交）
│   └── {角色}/
│       └── {角色}_{情感}.pt
├── output/               # 合成音频输出（不提交）
├── uploads/              # 上传的参考音频（不提交）
├── references/           # 参考音频（不提交）
├── LICENSE
└── .gitignore
```

运行时自动创建 `pt/`、`output/`、`web/`、`uploads/` 目录。

---

## 音色文件（pt）约定

pt 文件按「角色 / 情感」两级目录组织：

```
pt/
  张琛/
    张琛_平静.pt
    张琛_愤怒.pt
  李四/
    李四_激动.pt
```

**命名规则**：`pt/{角色}/{角色}_{情感}.pt`

程序查找 pt 文件时按以下顺序匹配：

1. `{角色}_{情感}.pt`（标准命名）
2. `{情感}.pt`（备用命名）
3. 遍历目录，匹配文件 stem 去掉 `{角色}_` 前缀后的名字（兼容历史命名）

**命名限制**：`role` 和 `emotion` 只能是字母、数字、中文、下划线、横线，长度 1~30。

---

## Web 界面

### 角色台词编辑器（`/`）

按角色组织台词，选择情感后批量生成语音，输出保存在 `output/`，通过 `/static/audio/{文件名}` 访问。

### 音色打包器（`/maker`）

三个步骤：

1. **参考音频**：上传参考音频（支持 `wav / mp3 / flac / m4a / ogg / webm / aac`）
2. **音色信息**：填写角色、情感；可选填写参考文本
3. **音色库**：查看已存在的音色

填写参考文本时使用完整 prompt 模式，克隆效果更好；不填则使用 `x_vector_only` 模式（不需要参考文本，但音色相似度略低）。

---

## API 参考

服务默认监听 `0.0.0.0:8001`。所有 JSON 请求使用 `Content-Type: application/json`。

### GET /api/pt/list

按角色列出所有 pt 音色。

**响应示例**

```json
{
  "roles": {
    "张琛": ["平静", "愤怒"],
    "李四": ["激动"]
  },
  "count": 3,
  "role_count": 2,
  "dir": "E:\\proj\\tts-api-git\\qwen3-tts-base-web\\pt"
}
```

---

### POST /api/pt/create

从参考音频创建 pt 音色文件，保存到 `pt/{角色}/{角色}_{情感}.pt`。

**请求体**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `ref_audio` | string | ✅ | - | 参考音频的服务器路径（先用 `/api/pt/upload` 上传） |
| `ref_text` | string | ❌ | `""` | 参考音频对应的文本；为空时使用 `x_vector_only` 模式 |
| `role` | string | ✅ | - | 角色名 |
| `emotion` | string | ✅ | - | 情感名 |
| `overwrite` | bool | ❌ | `false` | 是否覆盖已存在的 pt 文件 |

**请求示例**

```json
{
  "ref_audio": "E:\\proj\\tts-api-git\\qwen3-tts-base-web\\uploads\\a1b2c3d4.wav",
  "ref_text": "今天天气不错，我们出去走走吧。",
  "role": "张琛",
  "emotion": "平静",
  "overwrite": false
}
```

**响应示例**

```json
{
  "status": "ok",
  "role": "张琛",
  "emotion": "平静",
  "rel": "张琛/张琛_平静.pt",
  "path": "E:\\proj\\tts-api-git\\qwen3-tts-base-web\\pt\\张琛\\张琛_平静.pt",
  "items": 1,
  "item_type": "VoiceClonePromptItem",
  "x_vector_only": false
}
```

**错误码**

| 状态码 | 说明 |
|---|---|
| 400 | 缺少 role/emotion；命名非法；文件已存在且未传 `overwrite` |
| 404 | 参考音频不存在 |
| 500 | 创建 prompt 失败 / 保存失败 |

---

### GET /api/pt/inspect

诊断 pt 文件的内部结构，便于排查兼容性问题。

**查询参数**

| 参数 | 说明 |
|---|---|
| `role` + `emotion` | 按角色和情感定位 |
| `name` | 直接指定相对于 `pt/` 的路径 |

两者择一。

**响应示例**

```json
{
  "role": "张琛",
  "emotion": "平静",
  "path": "张琛/张琛_平静.pt",
  "structure": [
    {
      "ref_spk_embedding": "Tensor[1, 512]",
      "ref_code": "Tensor[1, 128]",
      "x_vector_only_mode": "bool"
    }
  ]
}
```

---

### POST /api/tts

生成克隆语音。支持两种方式定位音色：

- 通过 `role + emotion`（推荐）
- 直接指定 `pt_file`（兼容旧接口，相对于 `pt/` 的路径）

**请求体**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `text` | string | ✅ | - | 待合成文本 |
| `role` | string | 条件 | `""` | 角色名；未指定 `pt_file` 时必填 |
| `emotion` | string | 条件 | `""` | 情感名；未指定 `pt_file` 时必填 |
| `language` | string | ❌ | `"Chinese"` | 语言 |
| `mode` | string | ❌ | `"url"` | `url` 返回 JSON + 音频 URL；`file` 直接返回 wav 文件 |
| `pt_file` | string | ❌ | `""` | 兼容旧接口，指定 pt 相对路径（优先级高于 role+emotion） |

**请求示例**

```json
{
  "role": "张琛",
  "emotion": "平静",
  "text": "你好，欢迎使用 Qwen3-TTS 语音克隆服务。",
  "language": "Chinese",
  "mode": "url"
}
```

**响应（`mode="url"`）**

```json
{
  "status": "ok",
  "filename": "3f2c1e...e9.wav",
  "url": "/static/audio/3f2c1e...e9.wav",
  "local_path": "E:\\proj\\tts-api-git\\qwen3-tts-base-web\\output\\3f2c1e...e9.wav",
  "role": "张琛",
  "emotion": "平静"
}
```

**响应（`mode="file"`）**：直接返回 `audio/wav` 文件流。

**错误码**

| 状态码 | 说明 |
|---|---|
| 400 | 缺少 role/emotion；`mode` 非 `url`/`file` |
| 404 | 找不到指定的 pt 文件（响应会列出前 20 个可用音色） |
| 500 | 生成失败 / 保存音频失败 |

**curl 示例**

```bash
curl -X POST http://localhost:8001/api/tts ^
  -H "Content-Type: application/json" ^
  -d "{\"role\":\"张琛\",\"emotion\":\"平静\",\"text\":\"你好\",\"mode\":\"url\"}"
```

---

### POST /api/pt/upload

上传参考音频，返回服务器路径，供 `/api/pt/create` 使用。

**请求**：`multipart/form-data`，字段名 `file`

**允许的扩展名**：`.wav` `.mp3` `.flac` `.m4a` `.ogg` `.webm` `.aac`

**响应示例**

```json
{
  "status": "ok",
  "path": "E:\\proj\\tts-api-git\\qwen3-tts-base-web\\uploads\\a1b2c3d4e5f6.wav",
  "filename": "a1b2c3d4e5f6.wav",
  "original_name": "ref.wav",
  "size": 245760
}
```

文件以随机 UUID 命名保存到 `uploads/`，避免重名。

---

### GET /api/pt/download

下载 pt 文件。

**查询参数**

| 参数 | 必填 | 说明 |
|---|---|---|
| `role` | ✅ | 角色名 |
| `emotion` | ✅ | 情感名 |

**响应**：`application/octet-stream` 文件流。

---

## 模型与精度

### 使用的模型

| 用途 | 模型 |
|---|---|
| 分词器 | `Qwen/Qwen3-TTS-Tokenizer-12Hz` |
| 预设音色 | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` |
| 克隆音色 | `Qwen/Qwen3-TTS-12Hz-1.7B-Base` |
| 制作音色 | `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` |

服务启动时加载 **Base** 模型（用于语音克隆），其余模型按需加载。

### 精度自动选择

`qwen3tts_common.py` 中的 `pick_dtype()` 根据显卡计算能力自动选择精度：

| 计算能力 | 架构示例 | 精度 |
|---|---|---|
| sm_80+ | Ampere / Ada / Hopper / Blackwell（RTX 30/40/50 系） | `bfloat16` |
| sm_75 及以下 | Turing / Volta（GTX 1660 Ti、RTX 20 系） | `float16` |
| CPU | - | `float32` |

Turing 及更老的卡不支持 bfloat16，硬用会报 `no kernel image is available`，因此自动降级为 float16。

### 注意力实现

默认使用 `sdpa`，避免强制导入 `flash_attn`。如果安装了 `flash-attn`，可以手动传入 `attn="flash_attention_2"` 获得更快推理。

### 离线模式

服务默认运行在离线模式，避免启动时联网校验：

```
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
HF_HUB_DISABLE_TELEMETRY=1
```

---

## 部署脚本说明

`deploy.ps1` 共 6 步：

| 步骤 | 内容 |
|---|---|
| 0 | 检测 NVIDIA GPU，校验显存与计算能力，选择 CUDA 分支 |
| 1 | 检查 Python 3.11（未安装则通过 scoop 安装） |
| 2 | 创建 `.venv`，写入国内 pip 镜像配置 |
| 2.5 | 检查已安装 torch，分支不一致则卸载重装 |
| 3 | 安装 PyTorch（torch / torchvision / torchaudio 三元组） |
| 4 | 通过 `uv` 安装项目依赖（锁定 torch 三元组 + transformers） |
| 4.5 | 校验关键包版本，不符合则强制修复；可选安装 flash-attn |
| 5 | 通过 ModelScope 下载 4 个模型（CLI → SDK → HF 镜像三重回退） |
| 6 | 输出完成信息 |

### CUDA / PyTorch 版本选择

| 显卡计算能力 | CUDA | torch |
|---|---|---|
| < 7.5（Pascal、Volta、Turing 部分） | cu118 | 2.4.1 |
| >= 7.5（Turing sm_75+、Ampere、Ada、Hopper） | cu126 | 2.6.0 |

### 固定依赖版本

- `transformers==4.57.3`（qwen-tts 强制要求）
- `torch` / `torchvision` / `torchaudio` 锁定 CUDA 分支

### 使用的镜像

| 用途 | 镜像 |
|---|---|
| pip | 清华 TUNA、阿里云 |
| PyTorch wheels | 上海交大（备选：南京大学、阿里云） |
| scoop | 中科大 |
| HuggingFace | hf-mirror.com |
| ModelScope | modelscope.cn |

---

## 依赖与许可

### 本项目依赖

- [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) — Apache 2.0
- [FastAPI](https://fastapi.tiangolo.com/) — MIT
- [Uvicorn](https://www.uvicorn.org/) — BSD
- [Pydantic](https://docs.pydantic.dev/) — MIT
- [ModelScope](https://modelscope.cn/) — Apache 2.0
- [PyTorch](https://pytorch.org/) — BSD

### 许可证

- 本项目使用 [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)，其遵循 **Apache 2.0** 许可证。分发本项目时请保留对 Qwen3-TTS 的署名和 Apache 2.0 许可证文本。
- 本项目自身许可证见 [LICENSE](LICENSE)。

---

## 常见问题

**Q：`git commit` 报 `Author identity unknown`**

配置 Git 身份：

```powershell
git config --global user.name "你的名字"
git config --global user.email "你的邮箱"
```

**Q：PowerShell 里 `Get-Content` 中文乱码**

终端编码问题，执行：

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001
```

**Q：`pt` 文件已提交过，现在想忽略**

`.gitignore` 只对未跟踪文件生效。需要先从索引移除：

```powershell
git rm -r --cached pt
git commit -m "remove pt from tracking"
```

**Q：显存不足**

- 关闭其他占用 GPU 的程序
- 考虑使用 `Qwen3-TTS-12Hz-0.6B` 轻量版模型
- 或使用云端 GPU 服务

**Q：找不到模型目录**

先运行部署脚本下载模型：

```powershell
.\deploy.ps1
```

---

## 致谢

- [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)
- [ModelScope](https://modelscope.cn/)
- [FastAPI](https://fastapi.tiangolo.com/)