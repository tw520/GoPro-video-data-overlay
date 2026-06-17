from __future__ import annotations

import math

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .config import AltitudeUnit, Corner, OverlaySettings, SpeedUnit
from .extract import TelemetryData, gps_track_until, interpolate_gps, total_distance_m
from .map_tiles import RouteMapRenderer, try_create_route_map_renderer


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """優先載入支援繁體中文的字型。"""
    if bold:
        candidates = [
            ("C:/Windows/Fonts/msjhbd.ttc", 0),
            ("C:/Windows/Fonts/msyhbd.ttc", 0),
            ("C:/Windows/Fonts/msjh.ttc", 0),
        ]
    else:
        candidates = [
            ("C:/Windows/Fonts/msjh.ttc", 0),
            ("C:/Windows/Fonts/msyh.ttc", 0),
            ("C:/Windows/Fonts/mingliu.ttc", 0),
            ("C:/Windows/Fonts/segoeui.ttf", None),
            ("C:/Windows/Fonts/arial.ttf", None),
            ("/System/Library/Fonts/PingFang.ttc", 0),
            ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0),
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
        ]
    for path, index in candidates:
        try:
            if index is None:
                return ImageFont.truetype(path, size)
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> None:
    x, y = xy
    shadow = (0, 0, 0, 180)
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        draw.text((x + dx, y + dy), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill)


def _corner_origin(
    corner: Corner,
    panel_w: int,
    panel_h: int,
    frame_w: int,
    frame_h: int,
    margin: int,
) -> tuple[int, int]:
    if corner == Corner.TOP_LEFT:
        return margin, margin
    if corner == Corner.TOP_RIGHT:
        return frame_w - panel_w - margin, margin
    if corner == Corner.BOTTOM_LEFT:
        return margin, frame_h - panel_h - margin
    return frame_w - panel_w - margin, frame_h - panel_h - margin


def _convert_speed(speed_ms: float, unit: SpeedUnit) -> tuple[float, str]:
    if unit == SpeedUnit.KMH:
        return speed_ms * 3.6, "km/h"
    if unit == SpeedUnit.MPH:
        return speed_ms * 2.23694, "mph"
    return speed_ms, "m/s"


def _convert_altitude(alt_m: float, unit: AltitudeUnit) -> tuple[float, str]:
    if unit == AltitudeUnit.FEET:
        return alt_m * 3.28084, "ft"
    return alt_m, "m"


def _draw_rounded_panel(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    fill: tuple[int, int, int, int],
    radius: int = 16,
) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill)


def _draw_speed_gauge(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    speed_value: float,
    max_speed: float,
    accent: tuple[int, int, int],
    radius: int = 46,
) -> None:
    cx, cy = center
    draw.arc(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        start=210,
        end=-30,
        fill=(70, 70, 70, 220),
        width=max(3, int(7 * radius / 46)),
    )
    ratio = min(max(speed_value / max(max_speed, 1.0), 0.0), 1.0)
    angle = math.radians(210 - ratio * 240)
    tx = cx + int(radius * math.cos(angle))
    ty = cy - int(radius * math.sin(angle))
    draw.line((cx, cy, tx, ty), fill=accent + (255,), width=max(2, int(5 * radius / 46)))
    dot = max(3, int(5 * radius / 46))
    draw.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill=accent + (255,))


class OverlayRenderer:
    def __init__(self, settings: OverlaySettings, telemetry: TelemetryData):
        self.settings = settings
        self.telemetry = telemetry
        overlay_scale = max(settings.overlay_scale, 0.3)
        text_scale = settings.font_scale * overlay_scale
        self.dashboard_width = int(settings.dashboard_width * overlay_scale)
        self.gps_map_size = int(settings.gps_map_size * overlay_scale)
        self.margin = int(settings.margin * overlay_scale)
        self.label_font = _load_font(int(16 * text_scale))
        self.value_font = _load_font(int(22 * text_scale))
        self.speed_font = _load_font(int(40 * text_scale), bold=True)
        self.unit_font = _load_font(int(18 * text_scale))
        self.gauge_radius = int(46 * overlay_scale)
        self.max_speed = self._estimate_max_speed()
        self.map_renderer: RouteMapRenderer | None = None
        if telemetry.has_gps and telemetry.gps_samples and settings.show_gps_track:
            self.map_renderer = try_create_route_map_renderer(
                telemetry.gps_samples,
                self.gps_map_size,
                progress_callback=settings.progress_callback,
                cancel_check=settings.cancel_check,
            )

    def _estimate_max_speed(self) -> float:
        if not self.telemetry.gps_samples:
            return 60.0
        peak = max(s.speed_ms for s in self.telemetry.gps_samples) * 3.6
        return max(peak * 1.2, 20.0)

    def _paste_map(
        self,
        overlay: Image.Image,
        track: list[tuple[float, float]],
        origin: tuple[int, int],
        accent: tuple[int, int, int],
    ) -> None:
        size = self.gps_map_size
        x0, y0 = origin
        if self.map_renderer:
            map_img = self.map_renderer.render(track, accent)
        else:
            map_img = Image.new("RGBA", (size, size), (30, 32, 38, 220))
            draw = ImageDraw.Draw(map_img)
            if len(track) >= 2:
                lats = [p[0] for p in track]
                lons = [p[1] for p in track]
                min_lat, max_lat = min(lats), max(lats)
                min_lon, max_lon = min(lons), max(lons)
                pad = 16
                inner = size - pad * 2
                lat_span = max(max_lat - min_lat, 1e-6)
                lon_span = max(max_lon - min_lon, 1e-6)
                scale = inner / max(lat_span, lon_span)

                def to_xy(lat: float, lon: float) -> tuple[int, int]:
                    x = pad + int((lon - min_lon) * scale)
                    y = size - pad - int((lat - min_lat) * scale)
                    return x, y

                points = [to_xy(lat, lon) for lat, lon in track]
                draw.line(points, fill=accent + (255,), width=3, joint="curve")
                cx, cy = points[-1]
                draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=(255, 70, 70, 255))

        mask = Image.new("L", (size, size), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle((0, 0, size, size), radius=14, fill=255)
        overlay.paste(map_img, (x0, y0), mask)

    def render(self, frame_bgr: np.ndarray, time_sec: float) -> np.ndarray:
        h, w = frame_bgr.shape[:2]
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        gps = interpolate_gps(self.telemetry.gps_samples, time_sec)

        alpha = int(255 * self.settings.panel_opacity)
        panel_bg = (16, 18, 24, alpha)
        accent = self.settings.accent_color
        text = self.settings.text_color

        if self.settings.show_gps_track and self.telemetry.has_gps:
            track = gps_track_until(self.telemetry.gps_samples, time_sec)
            map_size = self.gps_map_size
            ox, oy = _corner_origin(
                self.settings.gps_map_corner,
                map_size,
                map_size,
                w,
                h,
                self.margin,
            )
            self._paste_map(overlay, track, (ox, oy), accent)

        rows: list[tuple[str, str, str]] = []
        if self.settings.show_speed and gps:
            val, unit = _convert_speed(gps.speed_ms, self.settings.speed_unit)
            rows.append(("速度", f"{val:.1f}", unit))
        if self.settings.show_altitude and gps:
            val, unit = _convert_altitude(gps.alt_m, self.settings.altitude_unit)
            rows.append(("高度", f"{val:.0f}", unit))
        if self.settings.show_distance and self.telemetry.has_gps:
            dist = total_distance_m(self.telemetry.gps_samples, time_sec)
            rows.append(("距離", f"{dist / 1000:.2f}", "km"))

        if rows:
            panel_w = max(self.dashboard_width, int(340 * self.settings.overlay_scale))
            speed_block = int(118 * self.settings.overlay_scale) if self.settings.show_speed and gps else 0
            row_height = int(34 * self.settings.overlay_scale)
            panel_h = speed_block + len(rows) * row_height + int(20 * self.settings.overlay_scale)
            px, py = _corner_origin(
                self.settings.dashboard_corner,
                panel_w,
                panel_h,
                w,
                h,
                self.margin,
            )
            _draw_rounded_panel(draw, (px, py, px + panel_w, py + panel_h), panel_bg)

            y = py + int(12 * self.settings.overlay_scale)
            if self.settings.show_speed and gps:
                val, unit = _convert_speed(gps.speed_ms, self.settings.speed_unit)
                gauge_cx = px + int(58 * self.settings.overlay_scale)
                gauge_cy = y + int(62 * self.settings.overlay_scale)
                _draw_text(draw, (px + 16, y), "速度", self.label_font, (180, 180, 180, 255))
                _draw_speed_gauge(
                    draw,
                    (gauge_cx, gauge_cy),
                    val,
                    self.max_speed,
                    accent,
                    self.gauge_radius,
                )
                _draw_text(
                    draw,
                    (px + int(130 * self.settings.overlay_scale), y + int(36 * self.settings.overlay_scale)),
                    f"{val:.1f}",
                    self.speed_font,
                    text + (255,),
                )
                _draw_text(
                    draw,
                    (px + int(130 * self.settings.overlay_scale), y + int(82 * self.settings.overlay_scale)),
                    unit,
                    self.unit_font,
                    accent + (255,),
                )
                y += speed_block

            extra_rows = rows[1:] if (self.settings.show_speed and gps) else rows
            for label, value, unit in extra_rows:
                _draw_text(draw, (px + 16, y), label, self.label_font, (170, 170, 170, 255))
                _draw_text(draw, (px + 88, y - 2), value, self.value_font, text + (255,))
                _draw_text(draw, (px + panel_w - 72, y), unit, self.unit_font, accent + (255,))
                y += row_height

        overlay_bgr = cv2.cvtColor(np.array(overlay), cv2.COLOR_RGBA2BGRA)
        alpha_ch = overlay_bgr[:, :, 3:4].astype(np.float32) / 255.0
        rgb = overlay_bgr[:, :, :3].astype(np.float32)
        base = frame_bgr.astype(np.float32)
        blended = base * (1.0 - alpha_ch) + rgb * alpha_ch
        return blended.astype(np.uint8)
