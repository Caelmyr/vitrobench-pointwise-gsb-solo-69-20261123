"""Measure continuous exposed coating around a user-marked loss axis."""

import numpy as np
from PIL import ImageDraw
from scipy import ndimage

from .art import base_rgba
from .color import delta_e, linear_lab
from .coverage import reference_region, sample_chip
from .geometry import region_domain
from .model import InputError


EXPOSED = 0
FILM = 1
AMBIGUOUS = 2
UNAVAILABLE = 3


def scribe_samples(frame, start, end, stations, reach):
    extent = np.asarray(frame.extent, dtype=np.float64)
    scale = np.asarray(frame.scale or (1., 1.))
    endpoints = np.asarray([start, end]) * extent - .5
    delta = (endpoints[1] - endpoints[0]) * scale
    length = float(np.linalg.norm(delta))
    if length < 5 * float(scale.min()):
        raise InputError("Mark both ends of the scribe at least five pixels apart.")
    sideways = np.array([-delta[1], delta[0]]) / (length * scale)
    centers = np.linspace(endpoints[0], endpoints[1], stations)
    half = float(min(extent * scale) * reach)
    count = min(400, max(3, int(np.ceil(half / float(scale.min())))))
    offsets = np.linspace(-half, half, 2 * count + 1)
    xy = centers[:, None, :] + offsets[None, :, None] * sideways
    return xy, offsets, length


def sampled_classes(frame, tile, area, linear, options, coordinates):
    lab = linear_lab(linear)
    usable_reference_area = region_domain(frame.extent, tile)
    metal = reference_region(tile, 'bare', options['bare_region'])
    film = reference_region(tile, 'glazed', options['glazed_region'])
    metal_lab, _, metal_record = sample_chip(frame, lab, usable_reference_area, metal)
    film_lab, _, film_record = sample_chip(frame, lab, usable_reference_area, film)
    separation = float(delta_e(film_lab, metal_lab))
    if separation < 9:
        raise InputError("The film and clear glass samples need more color separation.")
    xy = coordinates
    yx = (xy[..., 1], xy[..., 0])
    support = area & frame.valid & ~frame.clipped
    trusted = ndimage.map_coordinates(support.astype(np.float32), yx, order=1, mode='constant', cval=0) > .999
    observed_color = np.stack([ndimage.map_coordinates(lab[..., c], yx, order=1, mode='nearest') for c in range(3)], axis=-1)
    film_distance = delta_e(observed_color, film_lab)
    metal_distance = delta_e(observed_color, metal_lab)
    margin = max(3., .10 * separation)
    labels = np.full(trusted.shape, UNAVAILABLE, dtype=np.uint8)
    labels[trusted] = AMBIGUOUS
    labels[trusted & (metal_distance - film_distance >= margin)] = FILM
    labels[trusted & (film_distance - metal_distance >= margin)] = EXPOSED
    return labels, {'metal': metal_record, 'film': film_record, 'separation_delta_e': round(separation, 3)}


def first_sound_edge(labels, indices):
    """Return the last exposed sample before two adjacent film readings."""
    last_exposed = None
    film_run = 0
    for index in indices:
        state = labels[index]
        if state in (UNAVAILABLE, AMBIGUOUS):
            return ('no image support' if state == UNAVAILABLE else 'uncertain color'), None
        if state == FILM:
            film_run += 1
            if film_run == 2:
                return 'observed', last_exposed
        else:
            last_exposed = index
            film_run = 0
    return 'beyond band', None


def station_side(labels, indices, offsets, xy):
    state, edge_index = first_sound_edge(labels, indices)
    return {
        'state': state,
        'width': round(abs(float(offsets[edge_index])), 3) if edge_index is not None else None,
        'edge': xy[edge_index].tolist() if edge_index is not None else None,
    }


def measure_scribe(frame, tile, area, linear, options):
    xy, offsets, length = scribe_samples(frame, options['start'], options['end'], options['stations'], options['half_band'])
    classes, references = sampled_classes(frame, tile, area, linear, options, xy)
    middle = len(offsets) // 2
    extent = np.asarray(frame.extent)
    unit = 'mm' if frame.scale else 'px'
    image = base_rgba(frame)
    draw = ImageDraw.Draw(image)
    draw.line([tuple(xy[0, middle]), tuple(xy[-1, middle])], fill=(250, 208, 90), width=2)
    rows = []
    for index, (labels, points) in enumerate(zip(classes, xy)):
        center = points[middle]
        if labels[middle] == EXPOSED:
            left = station_side(labels, range(middle, -1, -1), offsets, points)
            right = station_side(labels, range(middle, len(offsets)), offsets, points)
        else:
            status = {FILM: 'painted glass at axis', AMBIGUOUS: 'uncertain axis color', UNAVAILABLE: 'axis not visible'}[labels[middle]]
            left = {'state': status, 'width': None, 'edge': None}
            right = left.copy()
        width = round(left['width'] + right['width'], 3) if left['width'] is not None and right['width'] is not None else None
        for side, hue in ((left, (106, 222, 200)), (right, (140, 175, 243))):
            if side['edge'] is not None:
                edge = tuple(side['edge'])
                draw.line([tuple(center), edge], fill=hue, width=2)
                draw.ellipse((edge[0] - 2, edge[1] - 2, edge[0] + 2, edge[1] + 2), fill=hue)
                side['edge'] = np.round((np.asarray(edge) + .5) / extent, 6).tolist()
        if width is None:
            draw.ellipse((center[0] - 2, center[1] - 2, center[0] + 2, center[1] + 2), fill=(241, 145, 119))
        rows.append({
            'position': round(index * length / (len(xy) - 1), 3),
            'center': np.round((center + .5) / extent, 6).tolist(),
            'left': left,
            'right': right,
            'total_width': width,
        })
    totals = [item['total_width'] for item in rows if item['total_width'] is not None]
    return {
        'length_unit': unit,
        'axis_length': round(length, 3),
        'observed_stations': len(totals),
        'requested_stations': len(rows),
        'median_width': round(float(np.median(totals)), 3) if totals else None,
        'maximum_width': max(totals, default=None),
        'stations': rows,
        'references': references,
        'note': 'The measured band is exposed surface connected to the marked loss axis. Both sides need image evidence for a total width.',
    }, image
