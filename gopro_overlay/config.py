from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Literal


class Corner(str, Enum):
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"


class SpeedUnit(str, Enum):
    KMH = "km/h"
    MPH = "mph"
    MS = "m/s"


class AltitudeUnit(str, Enum):
    METERS = "m"
    FEET = "ft"


class QualityPreset(str, Enum):
    ORIGINAL = "original"
    UHD_4K = "4k"
    FHD_1080P = "1080p"
    HD_720P = "720p"


QUALITY_MAP: dict[QualityPreset, tuple[int | None, str]] = {
    QualityPreset.ORIGINAL: (None, "18"),
    QualityPreset.UHD_4K: (3840, "20"),
    QualityPreset.FHD_1080P: (1920, "22"),
    QualityPreset.HD_720P: (1280, "24"),
}


@dataclass
class OverlaySettings:
    """使用者可自訂的疊加設定。"""

    show_speed: bool = True
    show_altitude: bool = True
    show_gps_track: bool = True
    show_distance: bool = False

    dashboard_corner: Corner = Corner.BOTTOM_LEFT
    gps_map_corner: Corner = Corner.BOTTOM_RIGHT

    speed_unit: SpeedUnit = SpeedUnit.KMH
    altitude_unit: AltitudeUnit = AltitudeUnit.METERS

    quality: QualityPreset = QualityPreset.ORIGINAL
    font_scale: float = 1.0
    overlay_scale: float = 1.0
    panel_opacity: float = 0.75
    accent_color: tuple[int, int, int] = (0, 255, 210)
    text_color: tuple[int, int, int] = (255, 255, 255)

    dashboard_width: int = 340
    gps_map_size: int = 240
    margin: int = 24

    use_gpu: bool = False
    progress_callback: Callable[[str, float | None], None] | None = field(default=None, repr=False)
    cancel_check: Callable[[], bool] | None = field(default=None, repr=False)

    def corner_label(self, corner: Corner) -> str:
        labels = {
            Corner.TOP_LEFT: "左上角",
            Corner.TOP_RIGHT: "右上角",
            Corner.BOTTOM_LEFT: "左下角",
            Corner.BOTTOM_RIGHT: "右下角",
        }
        return labels[corner]


def settings_from_dict(data: dict) -> OverlaySettings:
    """從 GUI / CLI 字典建立設定物件。"""
    corner_map = {c.value: c for c in Corner}
    speed_map = {u.value: u for u in SpeedUnit}
    alt_map = {u.value: u for u in AltitudeUnit}
    quality_map = {q.value: q for q in QualityPreset}

    accent = data.get("accent_color", "#00FFD2")
    if isinstance(accent, str) and accent.startswith("#"):
        accent = (
            int(accent[1:3], 16),
            int(accent[3:5], 16),
            int(accent[5:7], 16),
        )

    return OverlaySettings(
        show_speed=bool(data.get("show_speed", True)),
        show_altitude=bool(data.get("show_altitude", True)),
        show_gps_track=bool(data.get("show_gps_track", True)),
        show_distance=bool(data.get("show_distance", False)),
        dashboard_corner=corner_map.get(
            data.get("dashboard_corner", Corner.BOTTOM_LEFT.value),
            Corner.BOTTOM_LEFT,
        ),
        gps_map_corner=corner_map.get(
            data.get("gps_map_corner", Corner.BOTTOM_RIGHT.value),
            Corner.BOTTOM_RIGHT,
        ),
        speed_unit=speed_map.get(data.get("speed_unit", SpeedUnit.KMH.value), SpeedUnit.KMH),
        altitude_unit=alt_map.get(
            data.get("altitude_unit", AltitudeUnit.METERS.value),
            AltitudeUnit.METERS,
        ),
        quality=quality_map.get(
            data.get("quality", QualityPreset.ORIGINAL.value),
            QualityPreset.ORIGINAL,
        ),
        font_scale=float(data.get("font_scale", 1.0)),
        overlay_scale=float(data.get("overlay_scale", 1.0)),
        panel_opacity=float(data.get("panel_opacity", 0.75)),
        accent_color=accent if isinstance(accent, tuple) else (0, 255, 210),
    )
