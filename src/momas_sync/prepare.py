"""Build separate import GPX only when both API boundaries validate a known offset."""
from datetime import datetime, timedelta, timezone
import io
import re
import xml.etree.ElementTree as ET
from .timestamps import analyze, parse_gpx

PREPARATION_VERSION = 1
BOUNDARY_TOLERANCE_SECONDS = 5


class TimeReview(Exception):
    def __init__(self, code, check=None):
        super().__init__(code)
        self.code = code
        self.check = check


def prepare_gpx(ride, body, app_timezone):
    try:
        check = analyze(ride, body, app_timezone)
    except (ValueError, OverflowError):
        raise TimeReview('invalid_or_ambiguous_time') from None
    check['preparation_version'] = PREPARATION_VERSION
    root = parse_gpx(body)
    points = [node for node in root.iter() if node.tag.split('}')[-1] == 'trkpt']
    point_times = [[node for node in point if node.tag.split('}')[-1] == 'time'] for point in points]
    if not points or any(len(times) != 1 or not times[0].text for times in point_times):
        raise TimeReview('missing_trackpoint_time', check)
    if check['gpx_has_naive_times'] or not check['gpx_times_monotonic']:
        raise TimeReview('invalid_gpx_timeline', check)
    start, end = check['start_delta_seconds'], check['end_delta_seconds']
    if start is None or end is None:
        raise TimeReview('missing_api_time', check)
    api_start = datetime.fromisoformat(check['api_start_utc'])
    api_end = datetime.fromisoformat(check['api_end_utc'])
    if api_end < api_start:
        raise TimeReview('invalid_api_timeline', check)
    # Do not infer arbitrary shifts. These are the verified cases: no error,
    # or the observed UTC+8 export error, validated independently per ride.
    offset = next((seconds for seconds in (0, 28800)
                   if abs(start - seconds) <= BOUNDARY_TOLERANCE_SECONDS
                   and abs(end - seconds) <= BOUNDARY_TOLERANCE_SECONDS), None)
    if offset is None:
        raise TimeReview('unrecognized_time_offset', check)
    check['correction_seconds'] = -offset
    if offset == 0:
        return body, check
    for _, (prefix, uri) in ET.iterparse(io.BytesIO(body), events=('start-ns',)):
        if not re.fullmatch(r'ns\d+', prefix):
            ET.register_namespace(prefix, uri)
    for node in root.iter():
        if node.tag.split('}')[-1] != 'time' or not node.text:
            continue
        try:
            timestamp = datetime.fromisoformat(node.text.replace('Z', '+00:00'))
        except ValueError:
            raise TimeReview('invalid_gpx_time', check) from None
        if timestamp.tzinfo is None:
            raise TimeReview('naive_gpx_time', check)
        timestamp = timestamp.astimezone(timezone.utc) - timedelta(seconds=offset)
        node.text = timestamp.isoformat().replace('+00:00', 'Z')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True), check
