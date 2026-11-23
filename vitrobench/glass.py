"""Registered photographs of leaded glass, measured in the panel plane."""

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .art import base_rgba, magnitude_overlay
from .photos import luminance
from .geometry import active_region, analysis_mask, region_domain
from .model import InputError


CAME_COLOR = (94, 210, 212, 255)
UNCERTAIN_COLOR = (249, 179, 88, 255)
PANE_COLOR = (144, 232, 174, 255)


def sampled_panel(frame, area):
    return analysis_mask(frame, area, minimum=48, allow_clipped=False)


def glass_panes(frame, area, linear, options):
    """Find visible cells separated by photographed dark strips."""
    height, width = area.shape
    support = sampled_panel(frame, area)
    light = luminance(linear)
    metal = support & (light <= options["cutoff"])
    # A sub-pixel hole in a lead line can result from interpolation at a seam.
    # Close only inside existing photographic support.
    sealed = ndimage.binary_closing(metal, iterations=1) & support
    labels, count = ndimage.label(support & ~(metal | sealed))
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    chosen = sorted(
        (label for label in range(1, count + 1) if sizes[label] >= options["minimum_area"]),
        key=lambda label: (sizes[label], -label),
        reverse=True,
    )
    boundary = ndimage.binary_dilation(~support) & support
    all_rects = ndimage.find_objects(labels)
    output = base_rgba(frame, linear)
    draw = ImageDraw.Draw(output)
    rows = []
    for pane, label in enumerate(chosen[:300], 1):
        rect = all_rects[label - 1]
        component = labels[rect] == label
        yy, xx = np.nonzero(component)
        top, left = rect[0].start, rect[1].start
        touches = np.any(boundary[rect] & component)
        x = float(np.median(xx)) + left
        y = float(np.median(yy)) + top
        bounds = [left, top, rect[1].stop - 1, rect[0].stop - 1]
        badge = UNCERTAIN_COLOR if touches else PANE_COLOR
        outline = component & ~ndimage.binary_erosion(component)
        layer = Image.new("RGBA", (component.shape[1], component.shape[0]), badge)
        alpha = Image.fromarray(np.where(outline, 220, 0).astype(np.uint8))
        output.paste(layer, (left, top), alpha)
        draw.text((round(x), round(y)), str(pane), fill=badge, stroke_width=2, stroke_fill=(25, 31, 31))
        rows.append({
            "pane": pane,
            "pixels": int(sizes[label]),
            "area_mm2": round(float(sizes[label]) * frame.scale[0] * frame.scale[1], 3) if frame.scale else None,
            "state": "support edge" if touches else "bounded",
            "bounds": [round(v / axis, 6) for v, axis in zip(bounds, (width, height, width, height))],
            "center": [round(x / width, 6), round(y / height, 6)],
            "transmitted_luma": round(float(np.median(light[rect][component])), 5),
        })
    evidence = {
        "rows": rows,
        "pane_count": len(chosen),
        "listed": len(rows),
        "bounded": sum(row["state"] == "bounded" for row in rows),
        "unobserved_px": int(np.count_nonzero(area & ~support)),
        "note": "A light cell against a support boundary may continue off the image. Use the map as a glass census, with judgment for very dark glass.",
    }
    return evidence, output


def station_coordinates(frame, options):
    width, height = frame.extent
    extent = np.array([width - 1, height - 1], dtype=np.float64)
    start = np.array(options["start"]) * extent
    end = np.array(options["end"]) * extent
    delta = end - start
    length = np.linalg.norm(delta)
    if length < 5:
        raise InputError("Mark a longer section of the came centerline.")
    scale = np.array(frame.scale or [1, 1])
    along = delta * scale
    direction = np.array([-along[1], along[0]]) / np.linalg.norm(along)
    step = np.min(scale)
    offsets = np.arange(-options["reach_px"], options["reach_px"] + 1) * step
    anchors = start + np.linspace(0, 1, options["stations"])[:, None] * delta
    coordinates = anchors[:, None, :] + offsets[None, :, None] * direction / scale
    return anchors, coordinates, offsets, "mm" if frame.scale else "px"


def reached_glass(dark, valid, middle, sense):
    for index in range(middle + sense, len(valid) if sense > 0 else -1, sense):
        if not valid[index]:
            return None, "missing support"
        if not dark[index]:
            return index, None
    return None, "edge outside band"


def came_width(frame, area, linear, options):
    """Read both sides of the dark metal strip perpendicular to a marked line."""
    support = sampled_panel(frame, area)
    anchors, positions, offsets, unit = station_coordinates(frame, options)
    uv = (positions[:, :, 1], positions[:, :, 0])
    valid = ndimage.map_coordinates(support.astype(np.uint8), uv, order=0, mode="constant").astype(bool)
    levels = ndimage.map_coordinates(luminance(linear), uv, order=1, mode="constant")
    metal = levels <= options["cutoff"]
    midpoint = len(offsets) // 2
    output = base_rgba(frame, linear)
    draw = ImageDraw.Draw(output)
    rows = []
    for station, (location, section, dark, observed) in enumerate(zip(anchors, positions, metal, valid), 1):
        row = {"station": station, "point_px": location.tolist(), "width": None, "edges_px": None}
        if not observed[midpoint]:
            row["state"] = "missing support"
        elif not dark[midpoint]:
            row["state"] = "center off came"
        else:
            left, left_error = reached_glass(dark, observed, midpoint, -1)
            right, right_error = reached_glass(dark, observed, midpoint, 1)
            row["state"] = left_error or right_error or "measured"
            if left is not None and right is not None:
                half_step = offsets[midpoint + 1] / 2
                row["width"] = round(float(offsets[right] - offsets[left] - 2 * half_step), 4)
                mark_a = (section[left] + section[left + 1]) / 2
                mark_b = (section[right] + section[right - 1]) / 2
                row["edges_px"] = [mark_a.tolist(), mark_b.tolist()]
                draw.line([tuple(mark_a), tuple(mark_b)], fill=CAME_COLOR, width=2)
        if row["width"] is None:
            x, y = location
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=UNCERTAIN_COLOR)
        rows.append(row)
    measure = [row["width"] for row in rows if row["state"] == "measured"]
    return {
        "rows": rows,
        "length_unit": unit,
        "measured_stations": len(measure),
        "median_width": round(float(np.median(measure)), 4) if measure else None,
        "note": "Only stations with a visible center and both photographed transitions are measured.",
    }, output


def clear_glass_support(frame, tile, reference_id):
    card = active_region(tile, reference_id)
    if card is None or card["purpose"] != "bare":
        raise InputError("Select a region marked as clear glass before reading transmission.")
    mask = region_domain(frame.extent, tile, card)
    return card, sampled_panel(frame, mask)


def transmitted_density(frame, tile, area, linear, options):
    """Measure relative transmission against clear glass in the same exposure."""
    clear_card, card_mask = clear_glass_support(frame, tile, options["clear_region"])
    light = luminance(linear)
    reference = float(np.median(light[card_mask]))
    floor = options["floor"]
    if reference <= floor * 2:
        raise InputError("The clear glass sample is too dark for a useful transmission reference.")
    support = sampled_panel(frame, area)
    resolved = support & (light >= floor)
    if not np.any(resolved):
        raise InputError("No measured glass in the selected area rises above the sensor floor.")
    density = np.zeros_like(light)
    density[resolved] = np.log10(reference / light[resolved])
    xs = np.nonzero(area.any(axis=0))[0]
    rows = []
    for index, columns in enumerate(np.array_split(xs, min(options["cells"], len(xs))), 1):
        span = slice(int(columns[0]), int(columns[-1]) + 1)
        values = density[:, span][resolved[:, span]]
        rows.append({
            "station": index,
            "columns": [span.start, span.stop],
            "resolvable_px": len(values),
            "below_floor_px": int(np.count_nonzero(support[:, span] & ~resolved[:, span])),
            "unobserved_px": int(np.count_nonzero(area[:, span] & ~support[:, span])),
            "median_density": round(float(np.median(values)), 5) if len(values) else None,
            "median_transmission_pct": round(float(np.median(np.power(10.0, -values))) * 100, 3) if len(values) else None,
        })
    shown = np.maximum(density, 0)
    image = magnitude_overlay(frame, shown, resolved, 1.25, unknown=area & ~resolved)
    return {
        "rows": rows,
        "reference": {"region": clear_card, "luma": round(reference, 6), "pixels": int(np.count_nonzero(card_mask))},
        "median_density": round(float(np.median(density[resolved])), 5),
        "note": "This is relative transmitted intensity in the recorded exposure. Unphotographed and sensor-floor cells have no density reading.",
    }, image
