from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import (
    AltitudeUnit,
    Corner,
    OverlaySettings,
    QualityPreset,
    SpeedUnit,
)
from .gps_io import build_telemetry, export_gps_file, gps_source_label
from .process import ProcessingError, process_video


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="將 GoPro GPS、速度、高度等資料疊加到影片上",
    )
    parser.add_argument("input", help="GoPro MP4 輸入檔")
    parser.add_argument("-o", "--output", help="輸出 MP4 路徑")
    parser.add_argument(
        "--dashboard-corner",
        choices=[c.value for c in Corner],
        default=Corner.BOTTOM_LEFT.value,
        help="儀表板位置",
    )
    parser.add_argument(
        "--gps-corner",
        choices=[c.value for c in Corner],
        default=Corner.BOTTOM_RIGHT.value,
        help="GPS 軌跡地圖位置",
    )
    parser.add_argument(
        "--quality",
        choices=[q.value for q in QualityPreset],
        default=QualityPreset.ORIGINAL.value,
    )
    parser.add_argument("--speed-unit", choices=[u.value for u in SpeedUnit], default=SpeedUnit.KMH.value)
    parser.add_argument(
        "--altitude-unit",
        choices=[u.value for u in AltitudeUnit],
        default=AltitudeUnit.METERS.value,
    )
    parser.add_argument("--no-speed", action="store_true")
    parser.add_argument("--no-altitude", action="store_true")
    parser.add_argument("--no-gps-track", action="store_true")
    parser.add_argument("--distance", action="store_true", help="顯示累積距離")
    parser.add_argument("--font-scale", type=float, default=1.0)
    parser.add_argument("--overlay-scale", type=float, default=1.0, help="疊加面板整體大小")
    parser.add_argument("--opacity", type=float, default=0.75)
    parser.add_argument("--import-gps", help="匯入外部 GPS 檔（JSON / GPX）")
    parser.add_argument(
        "--gps-time-offset",
        type=float,
        default=0.0,
        help="GPS 時間偏移（秒），用於對齊影片",
    )
    parser.add_argument("--export-gps", help="匯出 GPS 到指定路徑（副檔名 .json 或 .gpx）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"找不到檔案: {input_path}", file=sys.stderr)
        return 1

    if args.export_gps:
        telemetry = build_telemetry(str(input_path))
        if not telemetry.has_gps:
            print("此影片沒有可匯出的 GPS 資料。", file=sys.stderr)
            return 1
        fmt = "gpx" if Path(args.export_gps).suffix.lower() == ".gpx" else "json"
        out = export_gps_file(telemetry, str(input_path), args.export_gps, fmt=fmt)
        print(f"GPS 已匯出: {out}")
        print(telemetry.summary())
        return 0

    output_path = Path(args.output) if args.output else input_path.with_name(f"{input_path.stem}_overlay.mp4")

    settings = OverlaySettings(
        show_speed=not args.no_speed,
        show_altitude=not args.no_altitude,
        show_gps_track=not args.no_gps_track,
        show_distance=args.distance,
        dashboard_corner=Corner(args.dashboard_corner),
        gps_map_corner=Corner(args.gps_corner),
        speed_unit=SpeedUnit(args.speed_unit),
        altitude_unit=AltitudeUnit(args.altitude_unit),
        quality=QualityPreset(args.quality),
        font_scale=args.font_scale,
        overlay_scale=args.overlay_scale,
        panel_opacity=args.opacity,
        progress_callback=lambda msg, pct: print(
            f"[{pct * 100:5.1f}%] {msg}" if pct is not None else msg
        ),
    )

    try:
        telemetry = process_video(
            str(input_path),
            str(output_path),
            settings,
            external_gps_path=args.import_gps,
            gps_time_offset_sec=args.gps_time_offset,
        )
        print(f"\n完成: {output_path}")
        print(telemetry.summary())
        print(f"GPS 來源: {gps_source_label(telemetry)}")
        return 0
    except ProcessingError as exc:
        print(f"錯誤: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
