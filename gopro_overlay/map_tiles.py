from __future__ import annotations

import io
import math
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from PIL import Image, ImageDraw

from .extract import GpsSample
from .video_io import WORK_DIR

TILE_SIZE = 256
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
CACHE_DIR = WORK_DIR / "map_cache"
USER_AGENT = "GoProVideoOverlay/1.1 (local video overlay; contact via GitHub)"
MAX_TILES = 16
TILE_TIMEOUT_SEC = 8
TILE_DELAY_SEC = 0.55
MAP_LOAD_TIMEOUT_SEC = 45


class MapLoadError(Exception):
    """OpenStreetMap 瓦片無法載入。"""


class MapLoadCancelled(MapLoadError):
    """使用者終止地圖載入。"""


def _ssl_contexts() -> list[ssl.SSLContext]:
    contexts: list[ssl.SSLContext] = []
    try:
        import certifi

        contexts.append(ssl.create_default_context(cafile=certifi.where()))
    except ImportError:
        pass
    contexts.append(ssl.create_default_context())
    unverified = ssl.create_default_context()
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE
    contexts.append(unverified)
    return contexts


def _lat_lon_to_world_px(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    sin_lat = math.sin(math.radians(lat))
    scale = TILE_SIZE * (2**zoom)
    x = (lon + 180.0) / 360.0 * scale
    y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * scale
    return x, y


def _tile_bounds(
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    zoom: int,
) -> tuple[int, int, int, int, int]:
    x1, y1 = _lat_lon_to_world_px(max_lat, min_lon, zoom)
    x2, y2 = _lat_lon_to_world_px(min_lat, max_lon, zoom)
    tile_min_x = int(min(x1, x2) // TILE_SIZE)
    tile_max_x = int(max(x1, x2) // TILE_SIZE)
    tile_min_y = int(min(y1, y2) // TILE_SIZE)
    tile_max_y = int(max(y1, y2) // TILE_SIZE)
    count = (tile_max_x - tile_min_x + 1) * (tile_max_y - tile_min_y + 1)
    return tile_min_x, tile_max_x, tile_min_y, tile_max_y, count


def _pick_zoom(min_lat: float, max_lat: float, min_lon: float, max_lon: float, size: int) -> int:
    for zoom in range(17, 9, -1):
        x1, y1 = _lat_lon_to_world_px(max_lat, min_lon, zoom)
        x2, y2 = _lat_lon_to_world_px(min_lat, max_lon, zoom)
        span = max(abs(x2 - x1), abs(y2 - y1), 1.0)
        _, _, _, _, tile_count = _tile_bounds(min_lat, max_lat, min_lon, max_lon, zoom)
        if span <= size * 0.82 and tile_count <= MAX_TILES:
            return zoom
    return 10


def _fetch_tile(z: int, x: int, y: int) -> Image.Image:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{z}_{x}_{y}.png"
    if cache_path.exists():
        return Image.open(cache_path).convert("RGBA")

    url = TILE_URL.format(z=z, x=x, y=y)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None

    for ctx in _ssl_contexts():
        try:
            time.sleep(TILE_DELAY_SEC)
            with urllib.request.urlopen(req, timeout=TILE_TIMEOUT_SEC, context=ctx) as resp:
                data = resp.read()
            cache_path.write_bytes(data)
            return Image.open(io.BytesIO(data)).convert("RGBA")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            continue

    raise MapLoadError(f"無法下載地圖瓦片 ({z}/{x}/{y}): {last_error}")


def try_create_route_map_renderer(
    samples: list[GpsSample],
    size: int,
    *,
    progress_callback: Callable[[str, float | None], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> RouteMapRenderer | None:
    """嘗試建立 OSM 底圖渲染器；失敗時回傳 None 以改用簡化 GPS 軌跡。"""
    if len(samples) < 2:
        return None

    started = time.monotonic()

    def _report(message: str, pct: float | None) -> None:
        if progress_callback:
            progress_callback(message, pct)

    def _check_cancel() -> None:
        if cancel_check and cancel_check():
            raise MapLoadCancelled("處理已終止。您可以調整參數後重新匯出。")

    def _check_timeout() -> None:
        if time.monotonic() - started > MAP_LOAD_TIMEOUT_SEC:
            raise MapLoadError("地圖載入逾時")

    try:
        return RouteMapRenderer(
            samples,
            size,
            report_progress=_report,
            cancel_check=_check_cancel,
            timeout_check=_check_timeout,
        )
    except MapLoadCancelled:
        raise
    except MapLoadError as exc:
        _report(f"OpenStreetMap 無法載入，改用簡化 GPS 軌跡（{exc}）", 0.08)
        return None


class RouteMapRenderer:
    """以 OpenStreetMap 圖磚為底，繪製 GPS 軌跡。"""

    def __init__(
        self,
        samples: list[GpsSample],
        size: int,
        *,
        report_progress: Callable[[str, float | None], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
        timeout_check: Callable[[], None] | None = None,
    ):
        self.size = size
        self.samples = samples
        self._report = report_progress
        self._cancel_check = cancel_check
        self._timeout_check = timeout_check

        if len(samples) < 2:
            self.base = Image.new("RGBA", (size, size), (30, 32, 38, 230))
            self._to_local = lambda _lat, _lon: (size // 2, size // 2)
            return

        lats = [s.lat for s in samples]
        lons = [s.lon for s in samples]
        lat_pad = max((max(lats) - min(lats)) * 0.15, 0.0008)
        lon_pad = max((max(lons) - min(lons)) * 0.15, 0.0008)
        self.min_lat = min(lats) - lat_pad
        self.max_lat = max(lats) + lat_pad
        self.min_lon = min(lons) - lon_pad
        self.max_lon = max(lons) + lon_pad

        self.zoom = _pick_zoom(self.min_lat, self.max_lat, self.min_lon, self.max_lon, size)
        x1, y1 = _lat_lon_to_world_px(self.max_lat, self.min_lon, self.zoom)
        x2, y2 = _lat_lon_to_world_px(self.min_lat, self.max_lon, self.zoom)
        self.origin_x = min(x1, x2)
        self.origin_y = min(y1, y2)
        span_x = max(abs(x2 - x1), 1.0)
        span_y = max(abs(y2 - y1), 1.0)
        self.scale = min((size - 8) / span_x, (size - 8) / span_y)

        self.base = self._build_base_map()

    def _progress(self, message: str, current: int, total: int) -> None:
        if self._report:
            pct = 0.06 + 0.02 * (current / max(total, 1)) if total else 0.07
            self._report(message, pct)

    def _to_local(self, lat: float, lon: float) -> tuple[int, int]:
        wx, wy = _lat_lon_to_world_px(lat, lon, self.zoom)
        x = int((wx - self.origin_x) * self.scale) + 4
        y = int((wy - self.origin_y) * self.scale) + 4
        return x, y

    def _build_base_map(self) -> Image.Image:
        x1, y1 = _lat_lon_to_world_px(self.max_lat, self.min_lon, self.zoom)
        x2, y2 = _lat_lon_to_world_px(self.min_lat, self.max_lon, self.zoom)
        min_wx, max_wx = min(x1, x2), max(x1, x2)
        min_wy, max_wy = min(y1, y2), max(y1, y2)

        tile_min_x, tile_max_x, tile_min_y, tile_max_y, total_tiles = _tile_bounds(
            self.min_lat,
            self.max_lat,
            self.min_lon,
            self.max_lon,
            self.zoom,
        )
        if total_tiles > MAX_TILES:
            raise MapLoadError(f"所需地圖瓦片過多（{total_tiles} 片）")

        canvas_w = (tile_max_x - tile_min_x + 1) * TILE_SIZE
        canvas_h = (tile_max_y - tile_min_y + 1) * TILE_SIZE
        canvas = Image.new("RGBA", (canvas_w, canvas_h), (30, 32, 38, 255))

        tile_index = 0
        failed = 0
        for tx in range(tile_min_x, tile_max_x + 1):
            for ty in range(tile_min_y, tile_max_y + 1):
                if self._cancel_check:
                    self._cancel_check()
                if self._timeout_check:
                    self._timeout_check()
                tile_index += 1
                self._progress(
                    f"下載 OpenStreetMap 地圖 {tile_index}/{total_tiles}（已快取會略過）",
                    tile_index,
                    total_tiles,
                )
                try:
                    tile = _fetch_tile(self.zoom, tx, ty)
                except MapLoadError:
                    failed += 1
                    if failed > max(2, total_tiles // 3):
                        raise
                    tile = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (30, 32, 38, 255))
                px = (tx - tile_min_x) * TILE_SIZE
                py = (ty - tile_min_y) * TILE_SIZE
                canvas.paste(tile, (px, py))

        crop_x = int(min_wx - tile_min_x * TILE_SIZE)
        crop_y = int(min_wy - tile_min_y * TILE_SIZE)
        crop_w = int(max(max_wx - min_wx, 1))
        crop_h = int(max(max_wy - min_wy, 1))
        cropped = canvas.crop((crop_x, crop_y, crop_x + crop_w, crop_y + crop_h))
        resized = cropped.resize((self.size, self.size), Image.Resampling.LANCZOS)

        self.origin_x = min_wx
        self.origin_y = min_wy
        self.scale = self.size / max(crop_w, crop_h, 1.0)
        if self._report:
            self._report("OpenStreetMap 地圖載入完成", 0.08)
        return resized.convert("RGBA")

    def render(self, track: list[tuple[float, float]], accent: tuple[int, int, int]) -> Image.Image:
        img = self.base.copy()
        draw = ImageDraw.Draw(img)

        if len(track) < 2:
            draw.text((10, self.size // 2 - 8), "GPS", fill=(220, 220, 220, 255))
            return img

        points = [self._to_local(lat, lon) for lat, lon in track]
        draw.line(points, fill=accent + (255,), width=4, joint="curve")

        cx, cy = points[-1]
        r = 6
        draw.ellipse(
            (cx - r, cy - r, cx + r, cy + r),
            fill=(255, 70, 70, 255),
            outline=(255, 255, 255, 255),
            width=2,
        )
        return img
