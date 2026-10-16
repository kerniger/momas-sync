from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET
from .client import ApiError


def parse_gpx(body):
    try:
        text = body.decode("utf-8-sig")
    except UnicodeError:
        raise ApiError("GPX must use UTF-8 encoding") from None
    if "\x00" in text or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ApiError("Unsafe XML declarations in GPX")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise ApiError("Download is not valid GPX XML") from None
    if root.tag.split("}")[-1] != "gpx":
        raise ApiError("Download does not contain GPX")
    return root


def parse_time(value, app_timezone):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        number = float(value)
        if abs(number) >= 100_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, timezone.utc)
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        zone = ZoneInfo(app_timezone)
        if result.replace(tzinfo=zone, fold=0).utcoffset() != result.replace(tzinfo=zone, fold=1).utcoffset():
            raise ValueError("Ambiguous or nonexistent local timestamp")
        result = result.replace(tzinfo=zone)
    return result.astimezone(timezone.utc)


def analyze(ride, gpx, app_timezone):
    root = parse_gpx(gpx)
    times = []
    naive_gpx = False
    for point in root.iter():
        if point.tag.split("}")[-1] != "trkpt":
            continue
        for child in point:
            if child.tag.split("}")[-1] == "time" and child.text:
                raw = datetime.fromisoformat(child.text.replace("Z", "+00:00"))
                if raw.tzinfo is None:
                    naive_gpx = True
                else:
                    times.append(raw.astimezone(timezone.utc))
    start = parse_time(ride.get("startTime"), app_timezone)
    end = parse_time(ride.get("endTime"), app_timezone)
    fields = {node.tag.split("}")[-1].lower() for node in root.iter()}
    return {"ride_id": str(ride["id"]), "assumed_app_timezone": app_timezone,
            "api_start_original": ride.get("startTime"),
            "api_end_original": ride.get("endTime"),
            "api_start_utc": start.isoformat() if start else None,
            "api_end_utc": end.isoformat() if end else None,
            "gpx_first_utc": times[0].isoformat() if times else None,
            "gpx_last_utc": times[-1].isoformat() if times else None,
            "start_delta_seconds": (times[0] - start).total_seconds() if times and start else None,
            "end_delta_seconds": (times[-1] - end).total_seconds() if times and end else None,
            "gpx_has_naive_times": naive_gpx,
            "gpx_times_monotonic": all(a <= b for a, b in zip(times, times[1:])),
            "gpx_has_power": bool(fields & {"power", "watts"}),
            "gpx_has_cadence": bool(fields & {"cad", "cadence"})}
