#!/usr/bin/env python3

import argparse
import datetime as dt
import json
import pytz
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from bottle import SimpleTemplate

import reverse_geocoder

ISO = '%Y-%m-%dT%H:%M:%SZ'
MILES_PER_METER = 0.000621371
nan = float('nan')

def main(argv):
    template_path = argv[0]
    cache_dir = Path('./cache')
    output_dir = Path('./output')
    fit_paths = argv[1:]

    with open(template_path) as f:
        template_html = f.read()

    if not output_dir.is_dir():
        output_dir.mkdir()

    geocoder = CachingGeocoder(cache_dir / 'geocodings.json')

    summaries = []

    for path_string in fit_paths:
        input_path = Path(path_string)
        xml_path = cache_dir / (input_path.stem + '.xml')
        html_path = output_dir / (input_path.stem + '.html')
        if not xml_path.exists():
            convert_to_xml(input_path, xml_path)
        summary = process(xml_path, geocoder, template_html, html_path)
        summaries.append(summary)

    write_index(summaries, output_dir / 'index.html')
    geocoder.save()

class CachingGeocoder(object):
    def __init__(self, path):
        self.path = path
        try:
            self.data = json.load(path.open())
        except FileNotFoundError:
            self.data = {}

    def search(self, lat, lon):
        key = '%.6f %.6f' % (lat, lon)
        result = self.data.get(key)
        if result is not None:
            return result
        result = reverse_geocoder.search([(lat, lon)])
        self.data[key] = result
        return result

    def save(self):
        j = json.dumps(self.data)  # to catch all errors before opening file
        with self.path.open('w') as f:
            f.write(j)
            f.write('\n')

def convert_to_xml(input_path, output_path):
    print('Converting', input_path, '->', output_path)
    cmd = '/home/brandon/usr/lib/garmin/fit2tcx'
    with open(output_path, 'w') as f:
        xml = subprocess.run([cmd, input_path], stdout=f)

def process(xml_path, geocoder, template_html, output_path):
    xml = open(xml_path).read()
    xml = xml.replace(
        'xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"',
        '',
    )
    import xml.etree.ElementTree as etree
    x = etree.fromstring(xml)

    c = []

    icons = []
    next_mile = 0
    splits = []

    trackpoints = list(parse_trackpoints(x))

    lat0 = trackpoints[0].latitude_degrees
    lon0 = trackpoints[0].longitude_degrees

    geocodes = geocoder.search(lat0, lon0)
    geocode = geocodes[0]

    from timezonefinder import TimezoneFinder
    tf = TimezoneFinder()
    name = tf.timezone_at(lng=lon0, lat=lat0)
    tz = pytz.timezone(name)
    utc = pytz.utc
    print(tz)
    for p in trackpoints:
        p.time = utc.localize(p.time).astimezone(tz)

    route = [[p.latitude_degrees, p.longitude_degrees] for p in trackpoints]
    mileposts = list(compute_mileposts(trackpoints))

    icons = [
        {
            'lat': p.latitude_degrees,
            'lon': p.longitude_degrees,
            'label': '{:.0f} mi<br>{:%-I:%M} {}'.format(
                p.distance_meters * MILES_PER_METER,
                p.time,
                p.time.strftime('%p').lower(),
            ),
        }
        for p in mileposts
    ]

    def compute_splits(trackpoints, mileposts):
        previous = trackpoints[0]
        for milepost in mileposts + [trackpoints[-1]]:
            meters = milepost.distance_meters - previous.distance_meters
            duration = milepost.time - previous.time
            yield Split(
                start = previous.time,
                end = milepost.time,
                duration = duration,
                meters = meters,
                mph = mph(meters, duration),
            )
            previous = milepost

    splits = list(compute_splits(trackpoints, mileposts))

    meters = trackpoints[-1].distance_meters
    miles = meters * MILES_PER_METER
    duration = trackpoints[-1].time - trackpoints[0].time
    #mph = miles / duration.total_seconds() * 60 * 60

    template = SimpleTemplate(template_html)
    content = template.render(
        duration=duration,
        icons=json.dumps(icons),
        route=json.dumps(route),
        miles=miles,
        mph=mph(meters, duration),
        splits=splits,
        start=trackpoints[0].time,
    )

    #print(splits)

    with open(output_path, 'w') as f:
        f.write(content)

    print(geocode)

    return Summary(
        geocode=geocode,
        start=trackpoints[0].time,
        miles=miles,
        url=output_path.name,
    )

def write_index(summaries, output_path):
    with open('index.template.html') as f:
        template_html = f.read()

    template = SimpleTemplate(template_html)
    content = template.render(
        summaries=summaries,
    )

    with open(output_path, 'w') as f:
        f.write(content)

@dataclass
class Summary(object):
    geocode: dict
    start: dt.datetime
    miles: float
    url: str

@dataclass
class Trackpoint(object):
    time: dt.datetime
    altitude_meters: float = 0.0
    distance_meters: float = 0.0
    latitude_degrees: float = 0.0
    longitude_degrees: float = 0.0

@dataclass
class Split(object):
    start: dt.datetime
    end: dt.datetime
    duration: dt.timedelta
    meters: float = 0.0
    mph: float = 0.0

def mph(meters, duration):
    try:
        return meters * MILES_PER_METER / duration.total_seconds() * 60 * 60
    except ZeroDivisionError:
        return 0.0

def parse_trackpoints(document):
    elements = document.findall('.//Trackpoint')
    for t in elements:
        p = t.find('Position')
        a = t.find('AltitudeMeters')
        if a is None:
            continue
        yield Trackpoint(
            time = date_of(t, 'Time'),
            altitude_meters = float(t.find('AltitudeMeters').text),
            distance_meters = float_of(t, 'DistanceMeters'),
            latitude_degrees = float(p.find('LatitudeDegrees').text),
            longitude_degrees = float(p.find('LongitudeDegrees').text),
        )

def date_of(parent, name):
    element = parent.find(name)
    if element is None:
        s = '2019-11-11T01:02:03Z'
    else:
        s = e.text
    return dt.datetime.strptime(s, ISO)

def float_of(parent, name):
    element = parent.find(name)
    if element is None:
        return nan
    return float(element.text)

def compute_mileposts(datapoints):
    datapoints = list(datapoints)
    next_mile = 1
    previous_time = 0
    previous_miles = 0
    for point in datapoints:
        d = point
        miles = d.distance_meters * MILES_PER_METER
        while miles > next_mile:
            fraction = (next_mile - previous_miles) / (miles - previous_miles)
            delta = d.time - previous_time
            yield Trackpoint(
                time = previous_time + delta * fraction,
                distance_meters = next_mile / MILES_PER_METER,
                altitude_meters = interpolate(
                    previous_point.altitude_meters,
                    point.altitude_meters,
                    fraction,
                ),
                latitude_degrees = interpolate(
                    previous_point.latitude_degrees,
                    point.latitude_degrees,
                    fraction,
                ),
                longitude_degrees = interpolate(
                    previous_point.longitude_degrees,
                    point.longitude_degrees,
                    fraction,
                ),
            )
            print(fraction)
            next_mile += 1
        previous_time = d.time
        previous_miles = miles
        previous_point = point

def interpolate(a, b, fraction):
    return a + (b - a) * fraction

# def compute_splits(mileposts):
#

if __name__ == '__main__':
    main(sys.argv[1:])
