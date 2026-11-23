import math

import numpy as np

from .color import float_list, linear_lab
from .geometry import line_grid, measure_line_samples
from .model import InputError
from .photos import luminance


def profile_distance(delta, scale):
    px_length = float(np.linalg.norm(delta))
    if scale is None:
        return px_length, "px"
    return float(np.linalg.norm(delta * scale)), "mm"


def row_values(labs, intensity, distance, units, counts, endpoints):
    size = len(counts)
    axis = np.linspace(0, distance, size)
    fractions = np.linspace(0, 1, size)
    start, end = np.asarray(endpoints[0]), np.asarray(endpoints[1])
    placements = start[None, :] + (end - start)[None, :] * fractions[:, None]
    rows = []
    for index, samples in enumerate(counts):
        values = labs[index]
        rows.append({
            "position": round(float(axis[index]), 5),
            "position_unit": units,
            "relative_position": float_list(placements[index], 6),
            "samples": int(samples),
            "lightness": round(float(values[0]), 4) if samples else None,
            "a": round(float(values[1]), 4) if samples else None,
            "b": round(float(values[2]), 4) if samples else None,
            "intensity": round(float(intensity[index, 0]), 5) if samples else None,
        })
    return rows


def positive_runs(supported):
    transitions = np.diff(np.pad(supported.astype(np.int8), (1, 1)))
    starts = np.flatnonzero(transitions == 1)
    stops = np.flatnonzero(transitions == -1)
    return list(zip(starts, stops))


def profile_features(lab_means, support, distance):
    supported_indices = np.flatnonzero(support)
    values = lab_means[support, 0]
    first = int(supported_indices[0])
    last = int(supported_indices[-1])
    min_idx = int(supported_indices[np.argmin(values)])
    max_idx = int(supported_indices[np.argmax(values)])
    runs = positive_runs(support)
    gradients = []
    for start, stop in runs:
        if stop - start < 2:
            continue
        gradients.extend(np.diff(lab_means[start:stop, 0]).tolist())
    length = max(1, len(support) - 1)
    return {
        "first_valid": first,
        "last_valid": last,
        "minimum_lightness_position": round(min_idx * distance / length, 4),
        "maximum_lightness_position": round(max_idx * distance / length, 4),
        "minimum_lightness": round(float(np.min(values)), 4),
        "maximum_lightness": round(float(np.max(values)), 4),
        "largest_neighbor_lightness_change": round(float(np.max(np.abs(gradients))), 4) if gradients else None,
        "median_lightness": round(float(np.median(values)), 4),
        "valid_runs": [[int(start), int(stop)] for start, stop in runs],
    }


def measure_profile(frame, linear, domain, options):
    endpoints = [options["start"], options["end"]]
    coords, delta = line_grid(frame.extent, *endpoints, options["samples"], options["band_pixels"])
    supported = frame.valid & ~frame.clipped & domain
    lab, counts = measure_line_samples(linear_lab(linear), supported, coords)
    intensity, _ = measure_line_samples(luminance(linear), supported, coords)
    valid = counts > 0
    if np.count_nonzero(valid) < 3:
        raise InputError("Mark a line that crosses more of the valid specimen.")
    length, unit = profile_distance(delta, frame.scale)
    return {
        "endpoints": endpoints,
        "band_pixels": options["band_pixels"],
        "line_length": round(length, 5),
        "distance_unit": unit,
        "supported_stations": int(np.count_nonzero(valid)),
        "requested_stations": len(counts),
        "rows": row_values(lab, intensity, length, unit, counts, endpoints),
        "features": profile_features(lab, valid, length),
    }


def mapped_line_grid(extent, endpoints, count, band_width, mapper):
    """Station grid of a paired frame whose positions follow the saved alignment."""
    width, height = extent
    origin = np.array(endpoints[0]) * [width, height] - 0.5
    finish = np.array(endpoints[1]) * [width, height] - 0.5
    delta = finish - origin
    length = float(np.linalg.norm(delta))
    perpendicular = np.array([-delta[1], delta[0]]) / max(length, 1e-9)
    centers = origin[None, :] + np.linspace(0, 1, count)[:, None] * delta[None, :]
    unit = (centers + 0.5) / [width, height]
    mapped, center_ok = mapper(unit)
    mapped_px = np.asarray(mapped) * [width, height] - 0.5
    steps = max(int(math.ceil(band_width)), 1)
    transverse = np.linspace(-band_width / 2, band_width / 2, steps) if steps > 1 else np.array([0.0])
    grid = mapped_px[:, None, :] + transverse[None, :, None] * perpendicular[None, None, :]
    return (grid[..., 1], grid[..., 0]), center_ok


def measure_profile_paired(frame, linear, domain, options, length, unit, mapper=None):
    endpoints = [options["start"], options["end"]]
    if mapper is None:
        coords, _ = line_grid(frame.extent, *endpoints, options["samples"], options["band_pixels"])
        center_ok = None
    else:
        coords, center_ok = mapped_line_grid(frame.extent, endpoints, options["samples"], options["band_pixels"], mapper)
    supported = frame.valid & ~frame.clipped & domain
    lab, counts = measure_line_samples(linear_lab(linear), supported, coords)
    intensity, _ = measure_line_samples(luminance(linear), supported, coords)
    if center_ok is not None:
        counts = np.where(center_ok, counts, 0)
    valid = counts > 0
    size = len(counts)
    axis = np.linspace(0, length, size)
    rows = []
    for index in range(size):
        rows.append({
            "position": round(float(axis[index]), 5),
            "samples": int(counts[index]),
            "lightness": round(float(lab[index, 0]), 4) if counts[index] else None,
            "intensity": round(float(intensity[index, 0]), 5) if counts[index] else None,
        })
    missing = []
    for start, stop in positive_runs(~valid):
        missing.append([round(float(axis[start]), 4), round(float(axis[stop - 1]), 4)])
    return {
        "rows": rows,
        "compared_stations": int(np.count_nonzero(valid)),
        "missing_stations": int(size - np.count_nonzero(valid)),
        "missing_intervals": missing,
        "distance_unit": unit,
        "note": "Paired values come through the saved pair alignment at the same stations; the measured line length is unchanged.",
    }
