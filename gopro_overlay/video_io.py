from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
from pathlib import Path

WORK_DIR = Path(tempfile.gettempdir()) / "gopro_overlay_work"
OUTPUT_DIR = Path(tempfile.gettempdir()) / "gopro_overlay_output"


def default_output_dir() -> Path:
    """預設輸出到使用者影片資料夾。"""
    return Path.home() / "Videos" / "GoPro_Overlay"


def resolve_output_dir(raw: str | None) -> Path:
    """解析並建立使用者指定的輸出資料夾。"""
    text = (raw or "").strip()
    target = default_output_dir() if not text else Path(text).expanduser()
    target = target.resolve()
    if target.exists() and not target.is_dir():
        raise ValueError(f"輸出路徑必須是資料夾: {target}")
    target.mkdir(parents=True, exist_ok=True)
    return target


def normalize_gradio_video(value: object | None) -> str | None:
    """Gradio Video 元件可能回傳字串路徑或 dict。"""
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("video", "path", "name"):
            path = value.get(key)
            if path:
                return str(path)
        return None
    return str(value)


def ensure_local_video_path(video_input: object | None) -> str:
    """將 Gradio 上傳檔複製到本機工作目錄，避免 Windows 檔案鎖定。"""
    src = normalize_gradio_video(video_input)
    if not src:
        raise FileNotFoundError("未提供影片路徑")

    src_path = Path(src).resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"找不到影片: {src}")

    work_root = WORK_DIR.resolve()
    if src_path.parent == work_root:
        return str(src_path)

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(str(src_path.resolve()).encode()).hexdigest()[:16]
    dest = WORK_DIR / f"{cache_key}_{src_path.name}"

    if dest.exists() and dest.stat().st_size == src_path.stat().st_size:
        return str(dest)

    last_error: Exception | None = None
    for attempt in range(8):
        try:
            shutil.copy2(src_path, dest)
            return str(dest)
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))

    raise PermissionError(
        f"無法讀取影片（可能被其他程式占用）: {src_path.name}"
    ) from last_error
