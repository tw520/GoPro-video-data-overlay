from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import cv2
import numpy as np

from .config import QUALITY_MAP, OverlaySettings
from .extract import TelemetryData, find_ffmpeg
from .gps_io import build_telemetry
from .map_tiles import MapLoadCancelled
from .render import OverlayRenderer


class ProcessingError(Exception):
    pass


class ProcessingCancelled(ProcessingError):
    """使用者主動終止處理。"""
    pass


def _report(settings: OverlaySettings, message: str, progress: float | None = None) -> None:
    cb = settings.progress_callback
    if cb:
        cb(message, progress)


def _check_cancel(settings: OverlaySettings) -> None:
    check = settings.cancel_check
    if check and check():
        raise ProcessingCancelled("處理已終止。您可以調整參數後重新匯出。")


def _cleanup_partial_output(output_path: str) -> None:
    path = Path(output_path)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def _open_video(path: str) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ProcessingError(f"無法開啟影片: {path}")
    return cap


def _video_info(cap: cv2.VideoCapture) -> tuple[int, int, float, int]:
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    return width, height, fps, frames


def _check_output_space(output_path: str, input_path: str) -> None:
    """確認輸出磁碟有足夠空間（約需與原片相近的容量）。"""
    try:
        src_size = Path(input_path).stat().st_size
    except OSError:
        return
    dest_dir = Path(output_path).parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dest_dir).free
    needed = int(src_size * 1.2) + 256 * 1024 * 1024
    if free < needed:
        free_gb = free / (1024**3)
        need_gb = needed / (1024**3)
        raise ProcessingError(
            f"輸出磁碟空間不足（可用 {free_gb:.1f} GB，估計需要 {need_gb:.1f} GB）。"
            f"請清理空間或更換輸出資料夾。"
        )


def _build_ffmpeg_cmd(
    ffmpeg: str,
    output_path: str,
    width: int,
    height: int,
    fps: float,
    settings: OverlaySettings,
    source_path: str,
) -> list[str]:
    max_width, crf = QUALITY_MAP[settings.quality]
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
        "-i",
        source_path,
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
    ]
    if max_width and width > max_width:
        cmd.extend(["-vf", f"scale={max_width}:-2"])
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            crf,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-shortest",
            output_path,
        ]
    )
    return cmd


def _start_ffmpeg(cmd: list[str]) -> subprocess.Popen:
    """啟動 FFmpeg，並在背景清空 stderr，避免管道緩衝區塞滿造成死鎖。"""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        try:
            proc.stderr.read()
        except OSError:
            pass

    threading.Thread(target=_drain_stderr, daemon=True).start()
    return proc


def _report_frame_progress(
    settings: OverlaySettings,
    frame_idx: int,
    frame_count: int,
    label: str = "渲染與編碼",
) -> None:
    pct = 0.08 + 0.88 * (frame_idx / max(frame_count, 1))
    _report(settings, f"{label} {frame_idx}/{frame_count or '?'} 幀", pct)


def _stream_with_ffmpeg(
    cap: cv2.VideoCapture,
    renderer: OverlayRenderer,
    output_path: str,
    width: int,
    height: int,
    fps: float,
    frame_count: int,
    settings: OverlaySettings,
    source_path: str,
) -> None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        _stream_with_opencv(cap, renderer, output_path, width, height, fps, frame_count, settings)
        return

    cmd = _build_ffmpeg_cmd(
        ffmpeg, output_path, width, height, fps, settings, source_path
    )
    proc = _start_ffmpeg(cmd)
    assert proc.stdin is not None

    _report(settings, "開始渲染與編碼影片…", 0.081)

    frame_idx = 0
    cancelled = False
    try:
        while True:
            _check_cancel(settings)
            ok, frame = cap.read()
            if not ok:
                break
            time_sec = frame_idx / fps
            composed = renderer.render(frame, time_sec)
            try:
                proc.stdin.write(composed.tobytes())
            except OSError as exc:
                if settings.cancel_check and settings.cancel_check():
                    cancelled = True
                    break
                err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                if exc.errno == 28:
                    raise ProcessingError(
                        "磁碟空間不足。請清理空間或更換輸出資料夾。"
                    ) from exc
                raise ProcessingError(f"寫入 FFmpeg 失敗:\n{err[-1500:]}") from exc

            frame_idx += 1
            if frame_idx == 1 or frame_idx % 10 == 0 or frame_idx == frame_count:
                _report_frame_progress(settings, frame_idx, frame_count)
    except ProcessingCancelled:
        cancelled = True
    finally:
        proc.stdin.close()
        if cancelled:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            _cleanup_partial_output(output_path)
            raise ProcessingCancelled("處理已終止。您可以調整參數後重新匯出。")

    code = proc.wait()
    if code != 0:
        raise ProcessingError(f"FFmpeg 編碼失敗（結束碼 {code}）")
    _verify_output_video(output_path)


def _stream_with_opencv(
    cap: cv2.VideoCapture,
    renderer: OverlayRenderer,
    output_path: str,
    width: int,
    height: int,
    fps: float,
    frame_count: int,
    settings: OverlaySettings,
) -> None:
    _report(settings, "未找到 FFmpeg，使用 OpenCV 編碼（無音訊）", 0.08)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
    if not writer.isOpened():
        raise ProcessingError("無法建立輸出影片。請安裝 FFmpeg。")

    _report(settings, "開始渲染影片…", 0.081)

    frame_idx = 0
    try:
        while True:
            _check_cancel(settings)
            ok, frame = cap.read()
            if not ok:
                break
            time_sec = frame_idx / fps
            composed = renderer.render(frame, time_sec)
            writer.write(composed)
            frame_idx += 1
            if frame_idx == 1 or frame_idx % 10 == 0 or frame_idx == frame_count:
                _report_frame_progress(settings, frame_idx, frame_count, label="渲染中")
    except ProcessingCancelled:
        _cleanup_partial_output(output_path)
        raise
    finally:
        writer.release()
    _verify_output_video(output_path)


def _verify_output_video(output_path: str) -> None:
    cap = cv2.VideoCapture(output_path)
    if not cap.isOpened():
        cap.release()
        raise ProcessingError(f"輸出影片無法開啟，可能編碼失敗: {output_path}")
    ok, _ = cap.read()
    cap.release()
    if not ok:
        raise ProcessingError(f"輸出影片沒有有效畫面: {output_path}")


def process_video(
    input_path: str,
    output_path: str,
    settings: OverlaySettings,
    telemetry: TelemetryData | None = None,
    external_gps_path: str | None = None,
    gps_time_offset_sec: float = 0.0,
) -> TelemetryData:
    """讀取 GoPro 影片，疊加資料後輸出。"""
    input_path = str(Path(input_path).resolve())
    output_path = str(Path(output_path).resolve())
    os.makedirs(Path(output_path).parent, exist_ok=True)
    _check_output_space(output_path, input_path)

    _report(settings, "正在解析 GPS 與遙測資料...", 0.02)
    if telemetry is None:
        telemetry = build_telemetry(
            input_path,
            external_gps_path=external_gps_path,
            time_offset_sec=gps_time_offset_sec,
        )

    if not telemetry.has_gps:
        raise ProcessingError(
            "未找到 GPS 資料。請確認影片含 GPMF GPS，或匯入 JSON / GPX 外部 GPS 檔。"
        )

    cap = _open_video(input_path)
    width, height, fps, frame_count = _video_info(cap)

    _report(settings, f"影片 {width}x{height} @ {fps:.2f} fps，共 {frame_count} 幀", 0.05)
    try:
        renderer = OverlayRenderer(settings, telemetry)
    except MapLoadCancelled as exc:
        raise ProcessingCancelled(str(exc)) from exc

    try:
        _stream_with_ffmpeg(
            cap,
            renderer,
            output_path,
            width,
            height,
            fps,
            frame_count,
            settings,
            input_path,
        )
        _report(settings, "完成！", 1.0)
        return telemetry
    finally:
        cap.release()


def preview_frame(
    input_path: str,
    settings: OverlaySettings,
    time_sec: float = 0.0,
    telemetry: TelemetryData | None = None,
    external_gps_path: str | None = None,
    gps_time_offset_sec: float = 0.0,
) -> np.ndarray:
    """產生單幀預覽（RGB）供 GUI 顯示。"""
    if telemetry is None:
        telemetry = build_telemetry(
            input_path,
            external_gps_path=external_gps_path,
            time_offset_sec=gps_time_offset_sec,
        )
    cap = _open_video(input_path)
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
    target = int(time_sec * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, target)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise ProcessingError("無法讀取預覽幀")
    rendered = OverlayRenderer(settings, telemetry).render(frame, time_sec)
    return cv2.cvtColor(rendered, cv2.COLOR_BGR2RGB)
