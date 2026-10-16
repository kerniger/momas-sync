"""Convert validated GPX to TCX while preserving explicit zero power samples."""
from datetime import datetime, timezone
import math
import xml.etree.ElementTree as ET
from .timestamps import parse_gpx

TCX = 'http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2'
EXT = 'http://www.garmin.com/xmlschemas/ActivityExtension/v2'
XSI = 'http://www.w3.org/2001/XMLSchema-instance'
VERSION = 1


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Nonfinite TCX value')
    return result


def integer(value, maximum):
    result = number(value)
    if result != int(result) or not 0 <= result <= maximum:
        raise ValueError('TCX sensor value out of range')
    return str(int(result))


def to_tcx(ride, gpx):
    root = parse_gpx(gpx)
    ET.register_namespace('', TCX)
    ET.register_namespace('ae', EXT)
    ET.register_namespace('xsi', XSI)
    output = ET.Element(f'{{{TCX}}}TrainingCenterDatabase')
    activities = ET.SubElement(output, f'{{{TCX}}}Activities')
    activity = ET.SubElement(activities, f'{{{TCX}}}Activity', {'Sport': 'Biking'})
    points = [point for point in root.iter() if point.tag.split('}')[-1] == 'trkpt']
    if not points:
        raise ValueError('No TCX trackpoints')

    def child(parent, tag, value=None, attrs=None):
        node = ET.SubElement(parent, f'{{{TCX}}}{tag}', attrs or {})
        if value is not None:
            node.text = str(value)
        return node

    def time_at(point):
        values = [node.text for node in point if node.tag.split('}')[-1] == 'time']
        if len(values) != 1 or not values[0]:
            raise ValueError('Missing TCX time')
        parsed = datetime.fromisoformat(values[0].replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Naive TCX time')
        return parsed.astimezone(timezone.utc)

    start, end = time_at(points[0]), time_at(points[-1])
    if end < start or any(time_at(a) > time_at(b) for a, b in zip(points, points[1:])):
        raise ValueError('Invalid TCX timeline')
    first = start.isoformat().replace('+00:00', 'Z')
    child(activity, 'Id', first)
    lap = child(activity, 'Lap', attrs={'StartTime': first})
    child(lap, 'TotalTimeSeconds', (end - start).total_seconds())
    # Bafang's trip unit is kilometres, validated against the live GPX export.
    distance = number(ride['trip']) * 1000
    if distance < 0:
        raise ValueError('Negative ride distance')
    child(lap, 'DistanceMeters', distance)
    calories = ride.get('calorie')
    child(lap, 'Calories', integer(calories, 65535) if calories is not None else '0')
    child(lap, 'Intensity', 'Active')
    child(lap, 'TriggerMethod', 'Manual')
    track = child(lap, 'Track')
    for point in points:
        tp = child(track, 'Trackpoint')
        child(tp, 'Time', time_at(point).isoformat().replace('+00:00', 'Z'))
        lat, lon = number(point.attrib['lat']), number(point.attrib['lon'])
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError('Invalid TCX position')
        position = child(tp, 'Position')
        child(position, 'LatitudeDegrees', point.attrib['lat'])
        child(position, 'LongitudeDegrees', point.attrib['lon'])
        values = {}
        for node in point:
            name = node.tag.split('}')[-1].lower()
            if name == 'ele' and node.text:
                number(node.text)
                child(tp, 'AltitudeMeters', node.text)
            if name == 'extensions':
                for extension in node.iter():
                    tag = extension.tag.split('}')[-1].lower()
                    if tag in ('cad', 'cadence', 'power', 'powerinwatts', 'hr', 'heartrate', 'heart_rate') and extension.text is not None:
                        values[tag] = extension.text
        heart = next((values[key] for key in ('hr','heartrate','heart_rate') if key in values), None)
        cadence = next((values[key] for key in ('cad','cadence') if key in values), None)
        power = next((values[key] for key in ('power','powerinwatts') if key in values), None)
        if heart is not None:
            child(child(tp, 'HeartRateBpm', attrs={f'{{{XSI}}}type':'HeartRateInBeatsPerMinute_t'}), 'Value', integer(heart, 255))
        if cadence is not None:
            child(tp, 'Cadence', integer(cadence, 255))
        if power is not None:
            extension = child(tp, 'Extensions')
            tpx = ET.SubElement(extension, f'{{{EXT}}}TPX')
            ET.SubElement(tpx, f'{{{EXT}}}Watts').text = integer(power, 65535)
    return ET.tostring(output, encoding='utf-8', xml_declaration=True)
