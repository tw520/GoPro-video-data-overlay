from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from telemetrik import extract_all_telemetry


@dataclass
class GpsSample:
    time_sec: float
    lat: float
    lon: float
    alt_m: float
    speed_ms: float
    fix: int


@dataclass
class TelemetryData:
    gps_samples: list[GpsSample] = field(default_factory=list)
    heart_rate: list[tuple[float, float]] = field(default_factory=list)
    available_streams: list[str] = field(default_factory=list)
    has_gps: bool = False
    has_heart_rate: bool = False

    def summary(self) -> str:
        parts = [f"可用資料流: {', '.join(self.available_streams) or '無'}"]
        if self.has_gps:
            source = "匯入 GPS" if "IMPORTED_GPS" in self.available_streams else "影片內建 GPS"
            parts.append(f"{source} 取樣點: {len(self.gps_samples)}")
        if self.has_heart_rate:
            parts.append(f"心率取樣點: {len(self.heart_rate)}")
        return " | ".join(parts)


HR_STREAM_KEYS = ("HRT", "HR", "HRTM", "HEAR", "HRTR")
GPS_STREAM_KEYS = ("GPS5", "GPS9", "GPSU", "GPSF")


def _pts_series(stream) -> list[tuple[float, object]]:
    if getattr(stream, "pts_data", None):
        return list(stream.pts_data)
    return [(ts / 1000.0, value) for ts, value in stream.data]


def _scalar_value(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)) and value:
        return float(value[0])
    return None


def _find_heart_rate_streams(streams: dict) -> list[tuple[str, object]]:
    found: list[tuple[str, object]] = []
    for key, stream in streams.items():
        upper = key.upper()
        name = (getattr(stream, "name", "") or "").lower()
        if upper in HR_STREAM_KEYS or "heart" in name or "hr" in name.split():
            found.append((key, stream))
    return found


def _parse_gps5_stream(stream) -> list[GpsSample]:
    samples: list[GpsSample] = []
    for time_sec, raw in _pts_series(stream):
        if not isinstance(raw, (list, tuple)) or len(raw) < 7:
            continue
        lat, lon, alt, spd2d, _spd3d, fix, _dop = raw[:7]
        if fix is None or int(fix) < 2:
            continue
        if lat == 0.0 and lon == 0.0:
            continue
        samples.append(
            GpsSample(
                time_sec=float(time_sec),
                lat=float(lat),
                lon=float(lon),
                alt_m=float(alt),
                speed_ms=max(float(spd2d), 0.0),
                fix=int(fix),
            )
        )
    return samples


def _parse_gps9_stream(stream) -> list[GpsSample]:
    """GPS9 欄位順序：lat, lon, alt, spd2d, spd3d, days, secs, dop, fix。"""
    samples: list[GpsSample] = []
    for time_sec, raw in _pts_series(stream):
        if not isinstance(raw, (list, tuple)) or len(raw) < 9:
            continue
        lat, lon, alt, spd2d, _spd3d, _days, _secs, _dop, fix = raw[:9]
        if fix is None or int(fix) < 2:
            continue
        if lat == 0.0 and lon == 0.0:
            continue
        samples.append(
            GpsSample(
                time_sec=float(time_sec),
                lat=float(lat),
                lon=float(lon),
                alt_m=float(alt),
                speed_ms=max(float(spd2d), 0.0),
                fix=int(fix),
            )
        )
    return samples


def _parse_gps_stream(stream, stream_key: str) -> list[GpsSample]:
    if stream_key == "GPS9":
        return _parse_gps9_stream(stream)
    return _parse_gps5_stream(stream)


def _embedded_speed_unreliable(samples: list[GpsSample]) -> bool:
    """GoPro 內建 speed 欄位常為 0 或極小值，但軌跡其實在移動。"""
    if len(samples) < 2:
        return True
    peak_embedded = max(s.speed_ms for s in samples)
    if peak_embedded >= 1.0:
        return False
    for i in range(1, len(samples)):
        a, b = samples[i - 1], samples[i]
        dt = b.time_sec - a.time_sec
        if dt <= 0:
            continue
        computed = haversine_m(a.lat, a.lon, b.lat, b.lon) / dt
        if computed > max(peak_embedded * 5, 1.0):
            return True
    return peak_embedded < 0.5


def _fill_speeds_from_track(samples: list[GpsSample]) -> list[GpsSample]:
    """以 GPS 軌跡時間差與距離推算速度（公尺/秒）。"""
    if len(samples) < 2:
        return samples

    speeds = [0.0] * len(samples)
    for i in range(1, len(samples)):
        a, b = samples[i - 1], samples[i]
        dt = b.time_sec - a.time_sec
        if dt <= 0:
            continue
        speeds[i] = haversine_m(a.lat, a.lon, b.lat, b.lon) / dt

    for i in range(1, len(samples) - 1):
        a, b = samples[i - 1], samples[i + 1]
        dt = b.time_sec - a.time_sec
        if dt > 0:
            speeds[i] = haversine_m(a.lat, a.lon, b.lat, b.lon) / dt

    if len(samples) > 1:
        speeds[0] = speeds[1]

    stop_threshold_ms = 0.4
    filled: list[GpsSample] = []
    for i, sample in enumerate(samples):
        speed_ms = speeds[i]
        if speed_ms < stop_threshold_ms:
            speed_ms = 0.0
        filled.append(
            GpsSample(
                time_sec=sample.time_sec,
                lat=sample.lat,
                lon=sample.lon,
                alt_m=sample.alt_m,
                speed_ms=speed_ms,
                fix=sample.fix,
            )
        )
    return filled


def extract_telemetry(video_path: str) -> TelemetryData:
    """從 GoPro MP4 提取 GPS 與心率等 GPMF 資料。"""
    streams = extract_all_telemetry(video_path)
    result = TelemetryData(available_streams=sorted(streams.keys()))

    for gps_key in GPS_STREAM_KEYS:
        if gps_key in streams:
            result.gps_samples = _parse_gps_stream(streams[gps_key], gps_key)
            if result.gps_samples:
                if _embedded_speed_unreliable(result.gps_samples):
                    result.gps_samples = _fill_speeds_from_track(result.gps_samples)
                result.has_gps = True
                break

    for _key, stream in _find_heart_rate_streams(streams):
        hr_points: list[tuple[float, float]] = []
        for time_sec, value in _pts_series(stream):
            bpm = _scalar_value(value)
            if bpm is not None and bpm > 0:
                hr_points.append((float(time_sec), float(bpm)))
        if hr_points:
            result.heart_rate = hr_points
            result.has_heart_rate = True
            break

    return result


def interpolate_scalar(
    points: list[tuple[float, float]],
    time_sec: float,
    default: float = 0.0,
) -> float:
    if not points:
        return default
    times = np.array([p[0] for p in points], dtype=np.float64)
    values = np.array([p[1] for p in points], dtype=np.float64)
    if time_sec <= times[0]:
        return float(values[0])
    if time_sec >= times[-1]:
        return float(values[-1])
    return float(np.interp(time_sec, times, values))


def interpolate_gps(
    samples: list[GpsSample],
    time_sec: float,
) -> GpsSample | None:
    if not samples:
        return None
    times = np.array([s.time_sec for s in samples], dtype=np.float64)
    if time_sec <= times[0]:
        return samples[0]
    if time_sec >= times[-1]:
        return samples[-1]

    idx = int(np.searchsorted(times, time_sec, side="right") - 1)
    idx = max(0, min(idx, len(samples) - 2))
    a, b = samples[idx], samples[idx + 1]
    span = b.time_sec - a.time_sec
    if span <= 0:
        return a
    t = (time_sec - a.time_sec) / span
    return GpsSample(
        time_sec=time_sec,
        lat=a.lat + (b.lat - a.lat) * t,
        lon=a.lon + (b.lon - a.lon) * t,
        alt_m=a.alt_m + (b.alt_m - a.alt_m) * t,
        speed_ms=a.speed_ms + (b.speed_ms - a.speed_ms) * t,
        fix=max(a.fix, b.fix),
    )


def gps_track_until(samples: list[GpsSample], time_sec: float) -> list[tuple[float, float]]:
    return [(s.lat, s.lon) for s in samples if s.time_sec <= time_sec]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlon / 2) ** 2
    return float(2 * r * np.arcsin(np.sqrt(a)))


def total_distance_m(samples: list[GpsSample], time_sec: float) -> float:
    visible = [s for s in samples if s.time_sec <= time_sec]
    if len(visible) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(visible)):
        total += haversine_m(
            visible[i - 1].lat,
            visible[i - 1].lon,
            visible[i].lat,
            visible[i].lon,
        )
    return total


def find_ffmpeg() -> str | None:
    import shutil

    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None
