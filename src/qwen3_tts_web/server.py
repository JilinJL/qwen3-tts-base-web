# HTTP API; model initialization belongs to the application lifespan.
"""
Qwen3-TTS Voice Clone API（角色/情感分级版）

pt 目录结构：
    pt/
      张琛/
        张琛_平静.pt
        张琛_愤怒.pt
      李四/
        李四_激动.pt
"""

import dataclasses
import inspect
import os
import re
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Dict, List

import soundfile as sf
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, BeforeValidator

from .config import load_settings
from .runtime import free, load

settings = load_settings()
BASE_DIR = settings.root


# ================= 目录 =================
PT_DIR = BASE_DIR / "pt"
OUT_DIR = BASE_DIR / "output"
WEB_DIR = Path(__file__).parent / "web"


# ================= 定位 VoiceClonePromptItem =================
def _find_prompt_item_cls():
    for mod_name in (
        "qwen_tts.inference.qwen3_tts_model",
        "qwen_tts.inference",
        "qwen_tts",
    ):
        try:
            mod = __import__(mod_name, fromlist=["VoiceClonePromptItem"])
            cls = getattr(mod, "VoiceClonePromptItem", None)
            if cls is not None:
                return cls, mod_name
        except Exception:
            continue
    return None, None


VOICE_CLONE_PROMPT_ITEM = None
model = None
inference_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app):
    global model, VOICE_CLONE_PROMPT_ITEM
    for directory in (PT_DIR, OUT_DIR, UPLOAD_DIR, BASE_DIR / "references"):
        directory.mkdir(parents=True, exist_ok=True)
    try:
        model = load(settings)
        VOICE_CLONE_PROMPT_ITEM, _ = _find_prompt_item_cls()
        app.state.ready = True
        yield
    finally:
        app.state.ready = False
        model = None
        free()


# ================= FastAPI =================
app = FastAPI(
    title="Qwen3-TTS Voice Clone API",
    description="按 角色/情感 组织 pt 文件，生成克隆语音",
    version="3.0.0",
    lifespan=lifespan,
)
app.state.ready = False
app.mount(
    "/static/audio", StaticFiles(directory=str(OUT_DIR), check_dir=False), name="audio"
)
app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")


@app.get("/api/health", summary="模型就绪状态")
def api_health():
    from fastapi.responses import JSONResponse

    from .models import MODELS

    ready = app.state.ready and model is not None
    return JSONResponse(
        {
            "ready": ready,
            "device": str(getattr(model, "device", settings.device)),
            "model": MODELS[settings.model_key],
        },
        status_code=200 if ready else 503,
    )


# ================= 请求模型 =================
def normalize_emotion(value):
    if value is None:
        return "平静"
    if isinstance(value, str):
        return value.strip() or "平静"
    return value


Emotion = Annotated[str, BeforeValidator(normalize_emotion)]


class TTSRequest(BaseModel):
    role: str = ""
    emotion: Emotion = "平静"
    text: str
    language: str = "Chinese"
    mode: str = "url"  # "url" | "file"
    pt_file: str = ""  # 兼容旧接口


class PromptCreateRequest(BaseModel):
    ref_audio: str
    ref_text: str = ""
    role: str
    emotion: Emotion = "平静"
    overwrite: bool = False


# ================= pt 扫描与定位 =================
def list_pt_roles() -> Dict[str, List[str]]:
    """扫描 pt/{角色}/{角色}_{情感}.pt，返回 {角色: [情感, ...]}（有序）"""
    result: Dict[str, List[str]] = {}
    if not PT_DIR.exists():
        return result

    for role_dir in sorted(PT_DIR.iterdir(), key=lambda p: p.name):
        if not role_dir.is_dir():
            continue
        role = role_dir.name
        prefix = role + "_"
        emotions: List[str] = []
        for f in sorted(role_dir.glob("*.pt"), key=lambda p: p.name):
            stem = f.stem
            emo = stem[len(prefix) :] if stem.startswith(prefix) else stem
            if emo not in emotions:
                emotions.append(emo)
        if emotions:
            result[role] = emotions
    return result


def resolve_pt_path(role: str, emotion: str):
    """根据 角色+情感 定位 .pt 文件；找不到返回 None。"""
    if not role or not emotion:
        return None
    role_dir = PT_DIR / role
    if not role_dir.is_dir():
        return None

    # ① 标准命名 {角色}_{情感}.pt
    p = role_dir / f"{role}_{emotion}.pt"
    if p.is_file():
        return p

    # ② 备用命名 {情感}.pt
    p = role_dir / f"{emotion}.pt"
    if p.is_file():
        return p

    # ③ 扫描匹配（兼容历史命名）
    prefix = role + "_"
    for f in role_dir.glob("*.pt"):
        stem = f.stem
        emo_name = stem[len(prefix) :] if stem.startswith(prefix) else stem
        if emo_name == emotion:
            return f
    return None


def flat_pt_names() -> List[str]:
    """扁平列出所有 '角色/情感'，用于错误提示。"""
    out = []
    for role, emos in list_pt_roles().items():
        for e in emos:
            out.append(f"{role}/{e}")
    return out


# ================= 工具函数 =================
def _dict_to_item(d: dict):
    cls = VOICE_CLONE_PROMPT_ITEM
    if cls is None:
        return SimpleNamespace(**d)

    if dataclasses.is_dataclass(cls):
        valid = {f.name for f in dataclasses.fields(cls)}
        kw = {k: v for k, v in d.items() if k in valid}
        try:
            return cls(**kw)
        except TypeError:
            for name in valid - kw.keys():
                kw[name] = None
            return cls(**kw)

    try:
        sig = inspect.signature(cls)
        valid = set(sig.parameters.keys())
        kw = {k: v for k, v in d.items() if k in valid}
        return cls(**kw)
    except Exception as e:
        print(f"[warn] 构造 {cls.__name__} 失败: {e}，退化到 SimpleNamespace")
        return SimpleNamespace(**d)


def _normalize_item(obj: Any):
    if isinstance(obj, dict):
        return _dict_to_item(obj)
    return obj


def _unwrap_prompt(obj: Any) -> List[Any]:
    if obj is None:
        raise ValueError("pt 文件内容为空")

    if (
        isinstance(obj, dict)
        and "items" in obj
        and isinstance(obj["items"], (list, tuple))
    ):
        print(f"[unwrap] 检测到 'items' 包装，解包 {len(obj['items'])} 个元素")
        obj = list(obj["items"])

    if isinstance(obj, (list, tuple)):
        items = list(obj)
    else:
        items = [obj]

    if not items:
        raise ValueError("pt 文件内容为空")
    return [_normalize_item(it) for it in items]


def load_prompt_from_path(p: Path) -> List[Any]:
    import torch

    if p is None or not p.is_file():
        raise HTTPException(404, detail=f"pt 文件不存在: {p}")

    try:
        obj = torch.load(str(p), map_location="cpu", weights_only=False)
    except Exception as e:
        raise HTTPException(500, detail=f"加载 pt 失败: {e}")

    try:
        prompt_list = _unwrap_prompt(obj)
    except Exception as e:
        raise HTTPException(500, detail=f"pt 内容非法: {e}")

    it0 = prompt_list[0]
    has_emb = getattr(it0, "ref_spk_embedding", None) is not None
    has_code = getattr(it0, "ref_code", None) is not None
    xonly = getattr(it0, "x_vector_only_mode", None)
    rel = p.relative_to(PT_DIR) if PT_DIR in p.parents else p.name

    print(
        f"[load_prompt] {rel} -> {len(prompt_list)} item(s), type={type(it0).__name__}"
    )
    print(
        f"[load_prompt] ref_spk_embedding={'yes' if has_emb else 'no'}, "
        f"ref_code={'yes' if has_code else 'no'}, x_vector_only_mode={xonly}"
    )
    return prompt_list


def _state_for_debug(prompt_list):
    it0 = prompt_list[0]
    if dataclasses.is_dataclass(it0):
        return {
            f.name: type(getattr(it0, f.name)).__name__ for f in dataclasses.fields(it0)
        }
    if isinstance(it0, SimpleNamespace):
        return {k: type(v).__name__ for k, v in vars(it0).items()}
    return {
        a: type(getattr(it0, a)).__name__ for a in dir(it0) if not a.startswith("_")
    }


# ================= API：pt 管理 =================
@app.get("/api/pt/list", summary="按角色列出所有 pt 音色")
def api_pt_list():
    roles = list_pt_roles()
    total = sum(len(v) for v in roles.values())
    return {
        "roles": roles,
        "count": total,
        "role_count": len(roles),
        "dir": str(PT_DIR),
    }


_SAFE = re.compile(r"^[\w\u4e00-\u9fa5\-]{1,30}$")


@app.post("/api/pt/create", summary="从参考音频创建 pt（保存到 角色/情感 目录）")
def api_pt_create(req: PromptCreateRequest):
    import torch

    if not os.path.exists(req.ref_audio):
        raise HTTPException(404, detail=f"参考音频不存在: {req.ref_audio}")
    if not req.role:
        raise HTTPException(400, detail="必须提供 role")
    if not _SAFE.match(req.role) or not _SAFE.match(req.emotion):
        raise HTTPException(
            400, detail="role/emotion 只能包含字母/数字/中文/下划线/横线"
        )
    role_dir = PT_DIR / req.role
    role_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{req.role}_{req.emotion}.pt"
    path = role_dir / fname

    if path.exists() and not req.overwrite:
        raise HTTPException(
            400, detail=f"文件已存在: {req.role}/{fname}（可传 overwrite=true 覆盖）"
        )

    kwargs = {"ref_audio": req.ref_audio}
    if req.ref_text:
        kwargs["ref_text"] = req.ref_text
    else:
        kwargs["x_vector_only_mode"] = True
        print("[create] 未提供 ref_text，使用 x_vector_only 模式")

    try:
        with inference_lock:
            prompt = model.create_voice_clone_prompt(**kwargs)
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(500, detail=f"创建 prompt 失败: {e}")

    prompt_list = _unwrap_prompt(prompt)
    print(f"[create] {len(prompt_list)} item(s), type={type(prompt_list[0]).__name__}")
    print(f"[create] 字段: {_state_for_debug(prompt_list)}")

    try:
        torch.save(prompt_list, str(path))
    except Exception as e:
        raise HTTPException(500, detail=f"保存 pt 失败: {e}")

    return {
        "status": "ok",
        "role": req.role,
        "emotion": req.emotion,
        "rel": f"{req.role}/{fname}",
        "path": str(path),
        "items": len(prompt_list),
        "item_type": type(prompt_list[0]).__name__,
        "x_vector_only": not bool(req.ref_text),
    }


@app.get("/api/pt/inspect", summary="诊断 pt 结构")
def api_pt_inspect(role: str = "", emotion: str = "平静", name: str = ""):
    import torch

    emotion = normalize_emotion(emotion)
    if role:
        p = resolve_pt_path(role, emotion)
        if p is None:
            raise HTTPException(404, detail=f"未找到: {role}/{emotion}")
    elif name:
        p = PT_DIR / name
        if not p.is_file():
            raise HTTPException(404, detail=f"pt 不存在: {name}")
    else:
        raise HTTPException(400, detail="需要 role 或 name")

    obj = torch.load(str(p), map_location="cpu", weights_only=False)

    def _describe(o, depth=0, max_depth=2):
        if depth > max_depth:
            return type(o).__name__
        if isinstance(o, dict):
            return {
                k: _describe(v, depth + 1, max_depth) for k, v in list(o.items())[:10]
            }
        if isinstance(o, (list, tuple)):
            return [_describe(o[0], depth + 1, max_depth)] if o else []
        if isinstance(o, torch.Tensor):
            return f"Tensor{tuple(o.shape)}"
        try:
            return {
                f.name: _describe(getattr(o, f.name), depth + 1, max_depth)
                for f in dataclasses.fields(o)
            }
        except Exception:
            pass
        return f"{type(o).__name__}"

    return {
        "role": role or None,
        "emotion": emotion or None,
        "path": str(p.relative_to(PT_DIR)),
        "structure": _describe(obj),
    }


# ================= API：TTS =================
@app.post("/api/tts", summary="生成语音（role+emotion 或 pt_file）")
def api_tts(req: TTSRequest):
    if req.mode not in ("file", "url"):
        raise HTTPException(400, detail="mode 必须是 'file' 或 'url'")
    # 定位 .pt
    if req.pt_file:
        p = PT_DIR / req.pt_file
    else:
        if not req.role:
            raise HTTPException(400, detail="必须提供 role")
        p = resolve_pt_path(req.role, req.emotion)
        if p is None:
            raise HTTPException(
                404,
                detail=f"未找到音色: {req.role}/{req.emotion}；可用: {flat_pt_names()[:20]}",
            )

    prompt_list = load_prompt_from_path(p)

    try:
        with inference_lock:
            wavs, sr = model.generate_voice_clone(
                text=req.text,
                language=req.language,
                voice_clone_prompt=prompt_list,
            )
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(500, detail=f"生成失败: {e}")

    fname = f"{uuid.uuid4().hex}.wav"
    out_path = OUT_DIR / fname
    try:
        sf.write(str(out_path), wavs[0], sr)
    except Exception as e:
        raise HTTPException(500, detail=f"保存音频失败: {e}")

    if req.mode == "file":
        return FileResponse(str(out_path), media_type="audio/wav", filename=fname)

    if req.mode == "url":
        return {
            "status": "ok",
            "filename": fname,
            "url": f"/static/audio/{fname}",
            "local_path": str(out_path),
            "role": req.role,
            "emotion": req.emotion,
        }

    raise HTTPException(400, detail="mode 必须是 'file' 或 'url'")


# ================= 上传 & maker 页面 =================
UPLOAD_DIR = BASE_DIR / "uploads"

ALLOWED_EXT = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".webm", ".aac"}


@app.post("/api/pt/upload", summary="上传参考音频，返回服务器路径")
async def api_pt_upload(file: UploadFile = File(...)):
    name = file.filename or "upload.wav"
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            400,
            detail=f"不支持的音频格式: {ext}（支持 {sorted(ALLOWED_EXT)}）",
        )

    safe = f"{uuid.uuid4().hex}{ext}"
    dst = UPLOAD_DIR / safe
    content = await file.read()
    dst.write_bytes(content)

    print(f"[upload] {name} -> {dst} ({len(content)} bytes)")
    return {
        "status": "ok",
        "path": str(dst),
        "filename": safe,
        "original_name": name,
        "size": len(content),
    }


@app.get("/api/pt/download", summary="下载 pt 文件")
def api_pt_download(role: str, emotion: str = "平静"):
    emotion = normalize_emotion(emotion)
    p = resolve_pt_path(role, emotion)
    if p is None:
        raise HTTPException(404, detail=f"未找到: {role}/{emotion}")
    return FileResponse(
        str(p),
        media_type="application/octet-stream",
        filename=p.name,
    )


@app.get("/maker", response_class=HTMLResponse, include_in_schema=False)
def maker_page():
    p = WEB_DIR / "maker.html"
    if not p.exists():
        return HTMLResponse(
            "<h3>web/maker.html 不存在</h3>"
            "<p>请把 maker.html 放到 <code>web/</code> 目录。</p>"
        )
    return HTMLResponse(p.read_text(encoding="utf-8"))


# ================= 测试页面 =================
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    idx = WEB_DIR / "index.html"
    if not idx.exists():
        return HTMLResponse(
            "<h3>web/index.html 不存在</h3>"
            "<p>请把测试页面放到 <code>web/index.html</code>，或访问 "
            "<a href='/docs'>/docs</a>。</p>"
        )
    return HTMLResponse(idx.read_text(encoding="utf-8"))


# ================= 启动 =================
if __name__ == "__main__":
    from .cli import main

    raise SystemExit(main(["serve"]))
