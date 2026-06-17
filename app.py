"""GoPro 影片資料疊加 — 圖形介面入口。"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from pathlib import Path
import shutil

import gradio as gr

from gopro_overlay.config import (
    AltitudeUnit,
    Corner,
    QualityPreset,
    SpeedUnit,
    settings_from_dict,
)
from gopro_overlay.extract import find_ffmpeg
from gopro_overlay.gps_io import (
    GpsImportError,
    build_telemetry,
    ensure_local_gps_path,
    export_gps_file,
    gps_source_label,
    import_gps_file,
)
from gopro_overlay.process import ProcessingCancelled, ProcessingError, preview_frame, process_video
from gopro_overlay.video_io import (
    WORK_DIR,
    default_output_dir,
    ensure_local_video_path,
    resolve_output_dir,
)

CORNER_CHOICES = [
    ("左上角", Corner.TOP_LEFT.value),
    ("右上角", Corner.TOP_RIGHT.value),
    ("左下角", Corner.BOTTOM_LEFT.value),
    ("右下角", Corner.BOTTOM_RIGHT.value),
]

QUALITY_CHOICES = [
    ("原始畫質", QualityPreset.ORIGINAL.value),
    ("4K (3840px)", QualityPreset.UHD_4K.value),
    ("1080p", QualityPreset.FHD_1080P.value),
    ("720p", QualityPreset.HD_720P.value),
]

GPS_EXPORT_CHOICES = [("JSON", "json"), ("GPX", "gpx")]

SPEED_CHOICES = [("公里/時", SpeedUnit.KMH.value), ("英里/時", SpeedUnit.MPH.value), ("公尺/秒", SpeedUnit.MS.value)]
ALT_CHOICES = [("公尺", AltitudeUnit.METERS.value), ("英尺", AltitudeUnit.FEET.value)]

_cancel_event = threading.Event()


def _format_progress(message: str, pct: float | None) -> str:
    if pct is None:
        return message
    return f"{pct * 100:.1f}% — {message}"


def _resolve_gps_path(gps_path: str | None) -> str | None:
    if not gps_path:
        return None
    return ensure_local_gps_path(gps_path)


def _analyze_video(video_path: str | None, gps_path: str | None, gps_time_offset: float) -> str:
    if not video_path:
        return "請上傳 GoPro MP4 影片。"
    try:
        local_path = ensure_local_video_path(video_path)
        external_gps = _resolve_gps_path(gps_path)
        data = build_telemetry(
            local_path,
            external_gps_path=external_gps,
            time_offset_sec=float(gps_time_offset or 0),
        )
        lines = [data.summary(), f"GPS 來源: {gps_source_label(data)}", f"工作檔案: {Path(local_path).name}"]
        if external_gps:
            lines.append(f"匯入檔案: {Path(external_gps).name}")
        if not data.has_gps:
            lines.append("⚠ 未偵測到 GPS。可匯入 JSON / GPX 外部 GPS 檔，或確認影片為含 GPMF 的原始 MP4。")
        return "\n".join(lines)
    except Exception as exc:
        return f"解析失敗: {exc}"


def _on_video_upload(
    video_path: str | None,
    gps_path: str | None,
    gps_time_offset: float,
) -> tuple[str | None, str]:
    """上傳後立即複製到工作目錄，避免 Gradio 暫存檔在 Windows 被鎖定。"""
    if not video_path:
        return None, "請上傳 GoPro MP4 影片。"
    try:
        local_path = ensure_local_video_path(video_path)
        return local_path, _analyze_video(local_path, gps_path, gps_time_offset)
    except Exception as exc:
        return None, f"上傳失敗: {exc}"


def _on_gps_import(gps_path: str | None, video_path: str | None, gps_time_offset: float) -> str:
    if not gps_path:
        if video_path:
            return _analyze_video(video_path, None, gps_time_offset)
        return "尚未匯入 GPS。將使用影片內建 GPS（若有）。"
    try:
        local_gps = _resolve_gps_path(gps_path)
        if video_path:
            return _analyze_video(video_path, local_gps, gps_time_offset)
        samples = import_gps_file(local_gps, float(gps_time_offset or 0))
        return f"已匯入 GPS：{Path(local_gps).name}（{len(samples)} 點）"
    except Exception as exc:
        return f"GPS 匯入失敗: {exc}"


def _export_gps_data(video_path: str | None, gps_export_format: str) -> tuple[str | None, str]:
    if not video_path:
        raise gr.Error("請先上傳 GoPro MP4 影片。")
    try:
        local_path = ensure_local_video_path(video_path)
        telemetry = build_telemetry(local_path)
        if not telemetry.has_gps:
            raise gr.Error("此影片沒有可匯出的 GPS 資料。")

        export_dir = WORK_DIR / "gps_export"
        export_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(local_path).stem
        fmt = gps_export_format.lower()
        ext = "gpx" if fmt == "gpx" else "gps.json"
        output_path = export_dir / f"{stem}.{ext}"
        export_gps_file(telemetry, local_path, str(output_path), fmt=fmt)
        return str(output_path), f"已匯出 GPS（{ext.upper()}）：{output_path.name}，共 {len(telemetry.gps_samples)} 點"
    except GpsImportError as exc:
        raise gr.Error(str(exc)) from exc
    except Exception as exc:
        raise gr.Error(f"GPS 匯出失敗: {exc}") from exc


def _build_settings(
    show_speed,
    show_altitude,
    show_gps_track,
    show_distance,
    dashboard_corner,
    gps_map_corner,
    speed_unit,
    altitude_unit,
    quality=None,
    font_scale=1.0,
    overlay_scale=1.0,
    panel_opacity=0.75,
    accent_color="#00FFD2",
):
    data = {
        "show_speed": show_speed,
        "show_altitude": show_altitude,
        "show_gps_track": show_gps_track,
        "show_distance": show_distance,
        "dashboard_corner": dashboard_corner,
        "gps_map_corner": gps_map_corner,
        "speed_unit": speed_unit,
        "altitude_unit": altitude_unit,
        "font_scale": font_scale,
        "overlay_scale": overlay_scale,
        "panel_opacity": panel_opacity,
        "accent_color": accent_color,
    }
    if quality is not None:
        data["quality"] = quality
    return settings_from_dict(data)


def _do_preview(
    video_path: str | None,
    gps_path: str | None,
    gps_time_offset: float,
    show_speed,
    show_altitude,
    show_gps_track,
    show_distance,
    dashboard_corner,
    gps_map_corner,
    speed_unit,
    altitude_unit,
    font_scale,
    overlay_scale,
    panel_opacity,
    accent_color,
    preview_time,
):
    if not video_path:
        return None, "請先上傳影片。"
    settings = _build_settings(
        show_speed,
        show_altitude,
        show_gps_track,
        show_distance,
        dashboard_corner,
        gps_map_corner,
        speed_unit,
        altitude_unit,
        font_scale=font_scale,
        overlay_scale=overlay_scale,
        panel_opacity=panel_opacity,
        accent_color=accent_color,
    )
    try:
        local_path = ensure_local_video_path(video_path)
        external_gps = _resolve_gps_path(gps_path)
        frame = preview_frame(
            local_path,
            settings,
            time_sec=float(preview_time),
            external_gps_path=external_gps,
            gps_time_offset_sec=float(gps_time_offset or 0),
        )
        source = "匯入 GPS" if external_gps else "影片內建 GPS"
        return frame, f"預覽已更新（使用{source}）。"
    except Exception as exc:
        return None, f"預覽失敗: {exc}"


def _pick_output_dir(current_dir: str) -> str:
    """開啟本機資料夾選擇視窗（僅限本機執行時）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        initial = current_dir.strip() or str(default_output_dir())
        selected = filedialog.askdirectory(title="選擇輸出資料夾", initialdir=initial)
        root.destroy()
        return selected or current_dir
    except Exception:
        return current_dir


def _prepare_ui_video(output_path: str) -> str:
    """複製輸出檔到 Gradio 可讀取的工作目錄，確保介面內可預覽。"""
    src = Path(output_path).resolve()
    preview_dir = WORK_DIR / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    dest = preview_dir / src.name
    if src != dest:
        shutil.copy2(src, dest)
    return str(dest)


def _request_cancel() -> str:
    _cancel_event.set()
    return "正在終止處理…"


def _do_export(
    video_path: str | None,
    gps_path: str | None,
    gps_time_offset: float,
    show_speed,
    show_altitude,
    show_gps_track,
    show_distance,
    dashboard_corner,
    gps_map_corner,
    speed_unit,
    altitude_unit,
    quality,
    font_scale,
    overlay_scale,
    panel_opacity,
    accent_color,
    output_dir,
    progress=gr.Progress(),
):
    if not video_path:
        raise gr.Error("請先上傳 GoPro MP4 影片。")

    settings = _build_settings(
        show_speed,
        show_altitude,
        show_gps_track,
        show_distance,
        dashboard_corner,
        gps_map_corner,
        speed_unit,
        altitude_unit,
        quality=quality,
        font_scale=font_scale,
        overlay_scale=overlay_scale,
        panel_opacity=panel_opacity,
        accent_color=accent_color,
    )

    progress_q: queue.Queue[str] = queue.Queue()
    result_box: dict[str, tuple] = {}
    error_box: dict[str, BaseException] = {}
    logs: list[str] = []

    _cancel_event.clear()
    settings.cancel_check = _cancel_event.is_set

    def _cb(msg: str, pct: float | None) -> None:
        text = _format_progress(msg, pct)
        logs.append(text)
        progress_q.put(text)
        if pct is not None:
            progress(pct, desc=text)

    settings.progress_callback = _cb

    def _worker() -> None:
        try:
            local_path = ensure_local_video_path(video_path)
            external_gps = _resolve_gps_path(gps_path)
            out_dir = resolve_output_dir(output_dir)
            stem = Path(local_path).stem
            output_path = out_dir / f"{stem}_overlay.mp4"
            telemetry = process_video(
                local_path,
                str(output_path),
                settings,
                external_gps_path=external_gps,
                gps_time_offset_sec=float(gps_time_offset or 0),
            )
            summary = telemetry.summary()
            ui_video = _prepare_ui_video(str(output_path))
            ffmpeg_note = find_ffmpeg() or "未找到"
            gps_note = gps_source_label(telemetry)
            log_text = "\n".join(
                logs
                + [
                    "",
                    summary,
                    f"GPS 來源: {gps_note}",
                    "",
                    f"FFmpeg: {ffmpeg_note}",
                    f"輸出資料夾: {out_dir}",
                    f"輸出檔案: {output_path}",
                    "",
                    "若右側無法播放，請用下方「下載輸出影片」或直接以播放器開啟輸出檔案。",
                ]
            )
            result_box["value"] = (ui_video, str(output_path), log_text)
        except BaseException as exc:
            error_box["exc"] = exc

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    latest = "0.0% — 準備開始…"
    yield None, None, latest, ""

    while worker.is_alive():
        try:
            while True:
                latest = progress_q.get_nowait()
                yield None, None, latest, ""
        except queue.Empty:
            pass
        time.sleep(0.15)

    try:
        while True:
            latest = progress_q.get_nowait()
            yield None, None, latest, ""
    except queue.Empty:
        pass

    if "exc" in error_box:
        exc = error_box["exc"]
        if isinstance(exc, ProcessingCancelled):
            yield None, None, "已終止", str(exc)
            return
        if isinstance(exc, (ValueError, ProcessingError)):
            raise gr.Error(str(exc)) from exc
        raise gr.Error(f"匯出失敗: {exc}\n{traceback.format_exc()}") from exc

    ui_video, output_path, log_text = result_box["value"]
    yield ui_video, output_path, "100.0% — 完成！", log_text


def build_app() -> gr.Blocks:
    with gr.Blocks(title="GoPro 影片資料疊加") as app:
        gr.Markdown(
            """
# GoPro 影片資料疊加
類似 **GoPro Quik**，將 GPS 軌跡、速度、高度等資料疊加到影片上。

**使用方式：** 上傳 GoPro 原始 MP4 →（可選）匯入外部 GPS → 調整顯示項目 → 預覽 → 匯出影片。

> 需使用含 GPMF 遙測軌道的原始 MP4，或匯入 JSON / GPX 外部 GPS 檔。建議安裝 [FFmpeg](https://www.gyan.dev/ffmpeg/builds/) 以獲得較佳輸出品質。
            """
        )

        with gr.Row():
            with gr.Column(scale=1):
                video_in = gr.File(
                    label="GoPro 輸入影片（MP4）",
                    file_types=[".mp4"],
                    type="filepath",
                )
                analyze_btn = gr.Button("分析遙測資料", variant="secondary")
                telemetry_info = gr.Textbox(label="遙測分析", lines=6, interactive=False)

                gr.Markdown("### GPS 資料")
                gps_import = gr.File(
                    label="匯入 GPS 資料（JSON / GPX，選填）",
                    file_types=[".json", ".gpx"],
                    type="filepath",
                )
                gps_time_offset = gr.Number(
                    value=0,
                    label="GPS 時間偏移（秒）",
                    info="若匯入軌跡與影片不同步，可調整此值（正數＝GPS 往後移）",
                )
                with gr.Row():
                    gps_export_format = gr.Dropdown(
                        GPS_EXPORT_CHOICES,
                        value="json",
                        label="匯出格式",
                    )
                    export_gps_btn = gr.Button("匯出 GPS 資料", variant="secondary")
                gps_export_file = gr.File(label="下載 GPS 資料", interactive=False)

                gr.Markdown("### 顯示項目")
                show_speed = gr.Checkbox(value=True, label="速度（含儀表）")
                show_altitude = gr.Checkbox(value=True, label="高度")
                show_gps_track = gr.Checkbox(value=True, label="GPS 軌跡地圖")
                show_distance = gr.Checkbox(value=False, label="累積距離")

                gr.Markdown("### 位置與單位")
                dashboard_corner = gr.Dropdown(CORNER_CHOICES, value=Corner.BOTTOM_LEFT.value, label="儀表板位置")
                gps_map_corner = gr.Dropdown(CORNER_CHOICES, value=Corner.BOTTOM_RIGHT.value, label="GPS 地圖位置")
                speed_unit = gr.Dropdown(SPEED_CHOICES, value=SpeedUnit.KMH.value, label="速度單位")
                altitude_unit = gr.Dropdown(ALT_CHOICES, value=AltitudeUnit.METERS.value, label="高度單位")

                gr.Markdown("### 輸出設定")
                output_dir = gr.Textbox(
                    label="輸出資料夾",
                    value=str(default_output_dir()),
                    placeholder=r"例如：D:\Videos\GoPro",
                )
                pick_output_btn = gr.Button("選擇資料夾...", variant="secondary")
                quality = gr.Dropdown(QUALITY_CHOICES, value=QualityPreset.ORIGINAL.value, label="輸出畫質")
                overlay_scale = gr.Slider(0.5, 2.0, value=1.0, step=0.05, label="疊加大小")
                font_scale = gr.Slider(0.7, 1.6, value=1.0, step=0.05, label="字體大小")
                panel_opacity = gr.Slider(0.4, 1.0, value=0.75, step=0.05, label="面板透明度")
                accent_color = gr.ColorPicker(value="#00FFD2", label="強調色")

                preview_time = gr.Slider(0, 60, value=5, step=0.5, label="預覽時間（秒）")
                preview_btn = gr.Button("更新預覽", variant="secondary")
                with gr.Row():
                    export_btn = gr.Button("匯出影片", variant="primary")
                    cancel_btn = gr.Button("終止處理", variant="stop")

            with gr.Column(scale=1):
                preview_img = gr.Image(label="疊加預覽", type="numpy")
                preview_status = gr.Textbox(label="預覽狀態", interactive=False)
                export_progress = gr.Textbox(label="處理進度", value="待機", interactive=False)
                video_out = gr.Video(label="輸出影片預覽")
                download_out = gr.File(label="下載輸出影片", interactive=False)
                export_log = gr.Textbox(label="處理紀錄", lines=12, interactive=False)

        gps_context = [video_in, gps_import, gps_time_offset]

        video_in.upload(
            _on_video_upload,
            inputs=gps_context,
            outputs=[video_in, telemetry_info],
        )
        analyze_btn.click(_analyze_video, inputs=gps_context, outputs=[telemetry_info])
        gps_import.upload(_on_gps_import, inputs=gps_context, outputs=[telemetry_info])
        gps_time_offset.change(_analyze_video, inputs=gps_context, outputs=[telemetry_info])

        export_gps_btn.click(
            _export_gps_data,
            inputs=[video_in, gps_export_format],
            outputs=[gps_export_file, telemetry_info],
        )

        pick_output_btn.click(_pick_output_dir, inputs=[output_dir], outputs=[output_dir])

        preview_inputs = [
            video_in,
            gps_import,
            gps_time_offset,
            show_speed,
            show_altitude,
            show_gps_track,
            show_distance,
            dashboard_corner,
            gps_map_corner,
            speed_unit,
            altitude_unit,
            font_scale,
            overlay_scale,
            panel_opacity,
            accent_color,
            preview_time,
        ]
        preview_btn.click(_do_preview, inputs=preview_inputs, outputs=[preview_img, preview_status])

        export_inputs = [
            video_in,
            gps_import,
            gps_time_offset,
            show_speed,
            show_altitude,
            show_gps_track,
            show_distance,
            dashboard_corner,
            gps_map_corner,
            speed_unit,
            altitude_unit,
            quality,
            font_scale,
            overlay_scale,
            panel_opacity,
            accent_color,
            output_dir,
        ]
        export_btn.click(
            _do_export,
            inputs=export_inputs,
            outputs=[video_out, download_out, export_progress, export_log],
        )
        cancel_btn.click(_request_cancel, outputs=[export_progress])

    return app


def _allowed_paths() -> list[str]:
    paths = {str(WORK_DIR.resolve()), str(Path.home().resolve())}
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = Path(f"{letter}:\\")
        if drive.exists():
            paths.add(str(drive))
    return sorted(paths)


def main() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    default_output_dir().mkdir(parents=True, exist_ok=True)
    app = build_app()
    app.launch(
        inbrowser=True,
        allowed_paths=_allowed_paths(),
    )


if __name__ == "__main__":
    main()
