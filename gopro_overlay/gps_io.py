from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .extract import (
    GpsSample,
    TelemetryData,
    _fill_speeds_from_track,
    extract_telemetry,
)
from .video_io import WORK_DIR

GPS_JSON_FORMAT = "gopro-overlay-gps"
GPS_JSON_VERSION = 1
IMPORTED_GPS_TAG = "IMPORTED_GPS"


class GpsImportError(ValueError):
    """GPS 匯入檔案格式或內容無效。"""


def ensure_local_gps_path(gps_input: object | None) -> str | None:
    """將 Gradio 上傳的 GPS 檔複製到工作目錄。"""
    if gps_input is None:
        return None
    if isinstance(gps_input, dict):
        path = gps_input.get("path") or gps_input.get("name")
        if not path:
            return None
        gps_input = path
    src_path = Path(str(gps_input)).resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"找不到 GPS 檔案: {src_path}")

    gps_dir = WORK_DIR / "gps_import"
    gps_dir.mkdir(parents=True, exist_ok=True)
    dest = gps_dir / src_path.name
    if src_path != dest:
        dest.write_bytes(src_path.read_bytes())
    return str(dest)


def _sample_to_dict(sample: GpsSample) -> dict:
    return {
        "time_sec": sample.time_sec,
        "lat": sample.lat,
        "lon": sample.lon,
        "alt_m": sample.alt_m,
        "speed_ms": sample.speed_ms,
        "fix": sample.fix,
    }


def _sample_from_dict(data: dict) -> GpsSample:
    return GpsSample(
        time_sec=float(data["time_sec"]),
        lat=float(data["lat"]),
        lon=float(data["lon"]),
        alt_m=float(data.get("alt_m", 0.0)),
        speed_ms=float(data.get("speed_ms", 0.0)),
        fix=int(data.get("fix", 3)),
    )


def _validate_samples(samples: list[GpsSample]) -> list[GpsSample]:
    if len(samples) < 2:
        raise GpsImportError("GPS 至少需要 2 個有效取樣點。")
    cleaned: list[GpsSample] = []
    for sample in samples:
        if not (-90.0 <= sample.lat <= 90.0 and -180.0 <= sample.lon <= 180.0):
            continue
        if sample.lat == 0.0 and sample.lon == 0.0:
            continue
        cleaned.append(sample)
    if len(cleaned) < 2:
        raise GpsImportError("GPS 有效取樣點不足。")
    cleaned.sort(key=lambda s: s.time_sec)
    return _fill_speeds_from_track(cleaned)


def export_gps_json(
    telemetry: TelemetryData,
    video_path: str,
    output_path: str,
    *,
    time_offset_sec: float = 0.0,
) -> str:
    if not telemetry.gps_samples:
        raise GpsImportError("沒有可匯出的 GPS 資料。")
    payload = {
        "format": GPS_JSON_FORMAT,
        "version": GPS_JSON_VERSION,
        "source_video": Path(video_path).name,
        "time_offset_sec": time_offset_sec,
        "sample_count": len(telemetry.gps_samples),
        "duration_sec": telemetry.gps_samples[-1].time_sec,
        "samples": [_sample_to_dict(s) for s in telemetry.gps_samples],
    }
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out.resolve())


def export_gps_gpx(
    telemetry: TelemetryData,
    video_path: str,
    output_path: str,
    *,
    time_offset_sec: float = 0.0,
) -> str:
    if not telemetry.gps_samples:
        raise GpsImportError("沒有可匯出的 GPS 資料。")

    base_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
    gpx = ET.Element(
        "gpx",
        {
            "version": "1.1",
            "creator": "GoProVideoOverlay",
            "xmlns": "http://www.topografix.com/GPX/1/1",
        },
    )
    metadata = ET.SubElement(gpx, "metadata")
    ET.SubElement(metadata, "name").text = Path(video_path).stem
    desc = ET.SubElement(metadata, "desc")
    desc.text = f"Exported from {Path(video_path).name}"

    trk = ET.SubElement(gpx, "trk")
    ET.SubElement(trk, "name").text = "GoPro GPS Track"
    seg = ET.SubElement(trk, "trkseg")

    for sample in telemetry.gps_samples:
        pt = ET.SubElement(
            seg,
            "trkpt",
            {"lat": f"{sample.lat:.7f}", "lon": f"{sample.lon:.7f}"},
        )
        ET.SubElement(pt, "ele").text = f"{sample.alt_m:.3f}"
        t = base_time.timestamp() + sample.time_sec + time_offset_sec
        ET.SubElement(pt, "time").text = (
            datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(gpx)
    ET.indent(tree, space="  ")
    tree.write(out, encoding="utf-8", xml_declaration=True)
    return str(out.resolve())


def export_gps_file(
    telemetry: TelemetryData,
    video_path: str,
    output_path: str,
    fmt: str = "json",
    *,
    time_offset_sec: float = 0.0,
) -> str:
    fmt_key = fmt.lower()
    if fmt_key == "gpx":
        return export_gps_gpx(
            telemetry,
            video_path,
            output_path,
            time_offset_sec=time_offset_sec,
        )
    if fmt_key == "json":
        return export_gps_json(
            telemetry,
            video_path,
            output_path,
            time_offset_sec=time_offset_sec,
        )
    raise GpsImportError(f"不支援的 GPS 匯出格式: {fmt}")


def _parse_iso_time(value: str) -> float:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


def _import_gps_json(path: Path, time_offset_sec: float) -> list[GpsSample]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and payload.get("format") == GPS_JSON_FORMAT:
        file_offset = float(payload.get("time_offset_sec", 0.0))
        raw_samples = payload.get("samples", [])
    elif isinstance(payload, list):
        file_offset = 0.0
        raw_samples = payload
    else:
        raise GpsImportError("JSON 格式無法辨識。請使用本工具匯出的 .gps.json 或含 samples 陣列的 JSON。")

    samples = [_sample_from_dict(item) for item in raw_samples]
    offset = time_offset_sec - file_offset
    if offset != 0.0:
        samples = [
            GpsSample(
                time_sec=s.time_sec + offset,
                lat=s.lat,
                lon=s.lon,
                alt_m=s.alt_m,
                speed_ms=s.speed_ms,
                fix=s.fix,
            )
            for s in samples
        ]
    return samples


def _import_gps_gpx(path: Path, time_offset_sec: float) -> list[GpsSample]:
    root = ET.parse(path).getroot()
    points: list[tuple[float, float, float, float | None]] = []

    for elem in root.iter():
        if elem.tag.split("}")[-1] != "trkpt":
            continue
        lat = elem.attrib.get("lat")
        lon = elem.attrib.get("lon")
        if lat is None or lon is None:
            continue
        alt_m = 0.0
        timestamp: float | None = None
        for child in elem:
            tag = child.tag.split("}")[-1]
            if tag == "ele" and child.text:
                alt_m = float(child.text)
            elif tag == "time" and child.text:
                timestamp = _parse_iso_time(child.text)
        points.append((float(lat), float(lon), alt_m, timestamp))

    if len(points) < 2:
        raise GpsImportError("GPX 檔案中沒有足夠的 trkpt 軌跡點。")

    if all(p[3] is not None for p in points):
        t0 = min(p[3] for p in points if p[3] is not None)
        samples = [
            GpsSample(
                time_sec=(ts - t0) + time_offset_sec,
                lat=lat,
                lon=lon,
                alt_m=alt_m,
                speed_ms=0.0,
                fix=3,
            )
            for lat, lon, alt_m, ts in points
            if ts is not None
        ]
    else:
        samples = [
            GpsSample(
                time_sec=float(i) + time_offset_sec,
                lat=lat,
                lon=lon,
                alt_m=alt_m,
                speed_ms=0.0,
                fix=3,
            )
            for i, (lat, lon, alt_m, _ts) in enumerate(points)
        ]
    return samples


def import_gps_file(path: str, time_offset_sec: float = 0.0) -> list[GpsSample]:
    src = Path(path).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"找不到 GPS 檔案: {src}")

    suffix = src.suffix.lower()
    if suffix == ".json":
        samples = _import_gps_json(src, time_offset_sec)
    elif suffix == ".gpx":
        samples = _import_gps_gpx(src, time_offset_sec)
    else:
        raise GpsImportError("僅支援 .json 或 .gpx GPS 檔案。")
    return _validate_samples(samples)


def apply_external_gps(telemetry: TelemetryData, samples: list[GpsSample]) -> TelemetryData:
    streams = [s for s in telemetry.available_streams if s != IMPORTED_GPS_TAG]
    streams.append(IMPORTED_GPS_TAG)
    return TelemetryData(
        gps_samples=samples,
        heart_rate=telemetry.heart_rate,
        available_streams=sorted(streams),
        has_gps=True,
        has_heart_rate=telemetry.has_heart_rate,
    )


def build_telemetry(
    video_path: str,
    external_gps_path: str | None = None,
    time_offset_sec: float = 0.0,
) -> TelemetryData:
    """解析影片遙測；若提供外部 GPS 檔，則改以匯入資料疊加。"""
    telemetry = extract_telemetry(video_path)
    if external_gps_path:
        samples = import_gps_file(external_gps_path, time_offset_sec=time_offset_sec)
        telemetry = apply_external_gps(telemetry, samples)
    return telemetry


def gps_source_label(telemetry: TelemetryData) -> str:
    if IMPORTED_GPS_TAG in telemetry.available_streams:
        return f"匯入 GPS（{len(telemetry.gps_samples)} 點）"
    if telemetry.has_gps:
        return f"影片內建 GPS（{len(telemetry.gps_samples)} 點）"
    return "無 GPS 資料"
