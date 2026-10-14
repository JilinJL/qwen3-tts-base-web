"""Windows-side PT asset library with soft delete and atomic replacement."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


NAME = re.compile(r"^[\w-]{1,60}$")
TRASH_ID = re.compile(r"^[a-f0-9]{32}$")
MAX_PT_BYTES = 32 * 1024 * 1024


class VoiceLibraryError(ValueError):
    """A client-visible asset library validation error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_base64(value: str, label: str, limit: int) -> bytes:
    if not isinstance(value, str):
        raise VoiceLibraryError(f"{label} must be base64 text")
    try:
        content = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VoiceLibraryError(f"invalid {label} base64") from exc
    if not content or len(content) > limit:
        raise VoiceLibraryError(f"{label} exceeds {limit // 1024 // 1024} MiB")
    return content


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


class VoiceLibrary:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.pt_root = (self.root / "pt").resolve()
        self.trash_root = (self.root / ".speakvideo" / "voice-trash").resolve()

    @staticmethod
    def validate_name(value: str, field: str) -> str:
        if not isinstance(value, str) or not NAME.fullmatch(value):
            raise VoiceLibraryError(f"invalid {field}")
        return value

    def _paths(self, role: str, emotion: str) -> dict[str, Path]:
        role = self.validate_name(role, "role")
        emotion = self.validate_name(emotion, "emotion")
        directory = (self.pt_root / role).resolve()
        if self.pt_root not in directory.parents:
            raise VoiceLibraryError("voice path escapes PT root")
        stem = f"{role}_{emotion}"
        return {
            "role": Path(role),
            "emotion": Path(emotion),
            "directory": directory,
            "pt": directory / f"{stem}.pt",
            "metadata": directory / f"{stem}.json",
            "reference": directory / f"{stem}.wav",
        }

    def _metadata(self, paths: dict[str, Path]) -> dict:
        if not paths["metadata"].is_file():
            return {}
        try:
            value = json.loads(paths["metadata"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _detail(self, paths: dict[str, Path]) -> dict:
        if not paths["pt"].is_file():
            raise VoiceLibraryError("voice asset not found")
        metadata = self._metadata(paths)
        return {
            "role": paths["role"].as_posix(),
            "emotion": paths["emotion"].as_posix(),
            "filename": paths["pt"].name,
            "sha256": self._sha256(paths["pt"]),
            "bytes": paths["pt"].stat().st_size,
            "has_reference": paths["reference"].is_file(),
            "ref_text": str(metadata.get("ref_text", "")),
            "notes": str(metadata.get("notes", "")),
            "origin": str(metadata.get("origin", "")),
        }

    def _validate_prompt(self, path: Path) -> None:
        try:
            from .base_agent import _validate_prompt_file
        except ImportError:
            from base_agent import _validate_prompt_file
        _validate_prompt_file(path)

    def list_assets(self) -> dict:
        assets = []
        if self.pt_root.is_dir():
            for role_dir in sorted(self.pt_root.iterdir(), key=lambda item: item.name):
                if not role_dir.is_dir() or not NAME.fullmatch(role_dir.name):
                    continue
                prefix = f"{role_dir.name}_"
                for path in sorted(role_dir.glob("*.pt"), key=lambda item: item.name):
                    stem = path.stem
                    if not stem.startswith(prefix):
                        continue
                    emotion = stem[len(prefix) :]
                    if not NAME.fullmatch(emotion):
                        continue
                    assets.append(self._detail(self._paths(role_dir.name, emotion)))
        return {"assets": assets, "source": "windows", "editable": True}

    def _archive(self, paths: dict[str, Path], operation: str) -> str:
        if not paths["pt"].is_file():
            raise VoiceLibraryError("voice asset not found")
        trash_id = uuid.uuid4().hex
        target = self.trash_root / trash_id
        target.mkdir(parents=True, exist_ok=False)
        moved = []
        try:
            for key, filename in (
                ("pt", "voice.pt"),
                ("metadata", "metadata.json"),
                ("reference", "reference.wav"),
            ):
                source = paths[key]
                if source.is_file():
                    os.replace(source, target / filename)
                    moved.append((target / filename, source))
            entry = {
                "id": trash_id,
                "role": paths["role"].as_posix(),
                "emotion": paths["emotion"].as_posix(),
                "operation": operation,
                "created_at": _utc_now(),
            }
            _atomic_json(target / "entry.json", entry)
            return trash_id
        except Exception:
            for archived, source in reversed(moved):
                if archived.exists() and not source.exists():
                    os.replace(archived, source)
            target.rmdir()
            raise

    def _check_sha(self, paths: dict[str, Path], expected: str) -> None:
        if not isinstance(expected, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected):
            raise VoiceLibraryError("sha256 is required")
        if not paths["pt"].is_file() or self._sha256(paths["pt"]) != expected.lower():
            raise VoiceLibraryError("voice SHA256 changed; refusing to modify a different PT")

    def prepare_create(self, payload: dict, content_base64: str) -> dict:
        """Validate a remote clone request and archive an overwritten asset."""

        role = self.validate_name(payload.get("role"), "role")
        emotion = self.validate_name(payload.get("emotion"), "emotion")
        if not str(payload.get("ref_text", "")).strip():
            raise VoiceLibraryError("ref_text is required")
        _decode_base64(content_base64, "reference", 32 * 1024 * 1024)
        paths = self._paths(role, emotion)
        if paths["pt"].exists():
            if not payload.get("overwrite", False):
                raise VoiceLibraryError("voice already exists; set overwrite=true")
            return {"paths": paths, "trash_id": self._archive(paths, "overwrite")}
        return {"paths": paths, "trash_id": None}

    def import_asset(self, payload: dict) -> dict:
        role = self.validate_name(payload.get("role"), "role")
        emotion = self.validate_name(payload.get("emotion"), "emotion")
        paths = self._paths(role, emotion)
        content = _decode_base64(payload.get("content_base64"), "PT", MAX_PT_BYTES)
        with tempfile.NamedTemporaryFile(
            dir=self.pt_root if self.pt_root.is_dir() else self.root,
            prefix=".voice-import-",
            suffix=".pt",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        try:
            self._validate_prompt(temporary)
        finally:
            temporary.unlink(missing_ok=True)
        if paths["pt"].exists():
            if not payload.get("overwrite", False):
                raise VoiceLibraryError("voice already exists; set overwrite=true")
            trash_id = self._archive(paths, "overwrite")
        else:
            trash_id = None
        _atomic_bytes(paths["pt"], content)
        _atomic_json(
            paths["metadata"],
            {
                "role": role,
                "emotion": emotion,
                "ref_text": str(payload.get("ref_text", "")),
                "notes": str(payload.get("notes", "")),
                "origin": str(payload.get("origin", "import")),
            },
        )
        paths["reference"].unlink(missing_ok=True)
        result = self._detail(paths)
        if trash_id:
            result["trash_id"] = trash_id
        return result

    def record_created(
        self, payload: dict, content_base64: str, *, result: dict | None = None
    ) -> dict:
        role = self.validate_name(payload.get("role"), "role")
        emotion = self.validate_name(payload.get("emotion"), "emotion")
        paths = self._paths(role, emotion)
        reference = _decode_base64(content_base64, "reference", 32 * 1024 * 1024)
        if not paths["pt"].is_file():
            raise VoiceLibraryError("Qwen created no PT asset")
        _atomic_json(
            paths["metadata"],
            {
                "role": role,
                "emotion": emotion,
                "ref_text": str(payload.get("ref_text", "")),
                "notes": str(payload.get("notes", "")),
                "origin": str(payload.get("origin", "reference-clone")),
            },
        )
        _atomic_bytes(paths["reference"], reference)
        detail = self._detail(paths)
        if result:
            detail["qwen"] = result
        return detail

    def edit(self, payload: dict) -> dict:
        old = self._paths(payload.get("role"), payload.get("emotion"))
        self._check_sha(old, payload.get("sha256", ""))
        new_role = self.validate_name(payload.get("new_role", payload.get("role")), "new_role")
        new_emotion = self.validate_name(
            payload.get("new_emotion", payload.get("emotion")), "new_emotion"
        )
        new = self._paths(new_role, new_emotion)
        if new["pt"] != old["pt"] and new["pt"].exists():
            raise VoiceLibraryError("target voice already exists")
        new["directory"].mkdir(parents=True, exist_ok=True)
        for key in ("pt", "metadata", "reference"):
            if old[key].is_file() and old[key] != new[key]:
                os.replace(old[key], new[key])
        metadata = self._metadata(new)
        metadata.update(
            {
                "role": new_role,
                "emotion": new_emotion,
                "ref_text": str(payload.get("ref_text", metadata.get("ref_text", ""))),
                "notes": str(payload.get("notes", metadata.get("notes", ""))),
            }
        )
        _atomic_json(new["metadata"], metadata)
        return self._detail(new)

    def delete(self, payload: dict) -> dict:
        if payload.get("permanent") is True:
            return self.delete_permanent(payload)
        paths = self._paths(payload.get("role"), payload.get("emotion"))
        if not paths["pt"].is_file():
            return {"deleted": True, "already_missing": True}
        self._check_sha(paths, payload.get("sha256", ""))
        trash_id = self._archive(paths, "delete")
        return {"deleted": True, "trash_id": trash_id, "remote_status": "deleted"}

    def delete_permanent(self, payload: dict) -> dict:
        """Permanently remove a PT asset and its sidecar files."""

        paths = self._paths(payload.get("role"), payload.get("emotion"))
        if not paths["pt"].is_file():
            return {
                "deleted": True,
                "permanent": True,
                "already_missing": True,
                "remote_status": "deleted",
            }
        self._check_sha(paths, payload.get("sha256", ""))
        detail = self._detail(paths)
        for key in ("metadata", "reference", "pt"):
            paths[key].unlink(missing_ok=True)
        return {
            "deleted": True,
            "permanent": True,
            "remote_status": "deleted",
            "asset": detail,
        }

    def reference(self, payload: dict) -> dict:
        paths = self._paths(payload.get("role"), payload.get("emotion"))
        self._check_sha(paths, payload.get("sha256", ""))
        if not paths["reference"].is_file():
            raise VoiceLibraryError("reference audio is not available")
        content = paths["reference"].read_bytes()
        if len(content) > 32 * 1024 * 1024:
            raise VoiceLibraryError("reference exceeds 32 MiB")
        return {"content_base64": base64.b64encode(content).decode("ascii")}

    def trash(self) -> dict:
        entries = []
        if self.trash_root.is_dir():
            for directory in sorted(self.trash_root.iterdir(), key=lambda item: item.name):
                entry = directory / "entry.json"
                if not directory.is_dir() or not entry.is_file() or not TRASH_ID.fullmatch(directory.name):
                    continue
                try:
                    value = json.loads(entry.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    entries.append(value)
        return {"trash": entries}

    def restore(self, payload: dict) -> dict:
        trash_id = payload.get("id", "")
        if not isinstance(trash_id, str) or not TRASH_ID.fullmatch(trash_id):
            raise VoiceLibraryError("invalid trash id")
        source = self.trash_root / trash_id
        try:
            entry = json.loads((source / "entry.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VoiceLibraryError("trash entry not found") from exc
        paths = self._paths(entry.get("role", ""), entry.get("emotion", ""))
        if paths["pt"].exists():
            raise VoiceLibraryError("target voice already exists")
        if not (source / "voice.pt").is_file():
            raise VoiceLibraryError("trash PT asset is missing")
        paths["directory"].mkdir(parents=True, exist_ok=True)
        for filename, target in (
            ("voice.pt", paths["pt"]),
            ("metadata.json", paths["metadata"]),
            ("reference.wav", paths["reference"]),
        ):
            item = source / filename
            if item.is_file():
                os.replace(item, target)
        (source / "entry.json").unlink(missing_ok=True)
        source.rmdir()
        return {"restored": True, "trash_id": trash_id, "asset": self._detail(paths)}

    def manage(self, payload: dict) -> dict:
        action = payload.get("action")
        if action == "list":
            return self.list_assets()
        if action == "import":
            return self.import_asset(payload)
        if action == "edit":
            return self.edit(payload)
        if action == "delete":
            return self.delete(payload)
        if action == "delete_permanent":
            return self.delete_permanent(payload)
        if action == "reference":
            return self.reference(payload)
        if action == "trash":
            return self.trash()
        if action == "restore":
            return self.restore(payload)
        raise VoiceLibraryError(f"unknown voice action: {action}")
