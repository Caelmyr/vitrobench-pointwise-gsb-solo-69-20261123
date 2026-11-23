"""Map supported dark openings on a registered, polished cut face."""

import numpy as np
from PIL import Image
from scipy import ndimage

from .art import base_rgba
from .model import InputError
from .photos import luminance


# A corner touch does not make a resolvable narrow throat at this pixel scale.
CONNECTIVITY = ndimage.generate_binary_structure(2, 1)
MAX_RETAINED_FEATURES = 500


def feature_record(state, identifier, locations, shape, scale):
    y, x = locations
    height, width = shape
    area = int(x.size)
    bounds = (
        (int(x.min()), int(y.min())),
        (int(x.max()) + 1, int(y.max()) + 1),
    )
    center = [round(float(np.mean(x + .5) / width), 6),
              round(float(np.mean(y + .5) / height), 6)]
    return {
        "id": identifier,
        "state": state,
        "pixels": area,
        "square_mm": round(area * scale[0] * scale[1], 5) if scale else None,
        "bounds_px": bounds,
        "center_normalized": center,
    }


def observation_map(frame, domain, support, layer):
    pixels = np.asarray(base_rgba(frame)).copy()
    colors = (
        (48, 221, 158),       # Closed within observed support.
        (232, 160, 66),       # Boundary truncated.
    )
    for index, color in enumerate(colors, start=1):
        region = layer == index
        pixels[region, :3] = np.rint(.41 * pixels[region, :3] + .59 * np.array(color)).astype(np.uint8)
    unmet = domain & ~support
    iy, ix = np.indices(domain.shape)
    hatched = unmet & ((ix + iy) % 8 < 3)
    pixels[hatched] = (229, 218, 209, 255)
    return Image.fromarray(pixels)


def measure_pores(frame, domain, linear, options):
    requested = int(np.count_nonzero(domain))
    valid = domain & frame.valid & ~frame.clipped
    observed = int(np.count_nonzero(valid))
    if observed < 64:
        raise InputError("Choose a coated area with at least 64 observed, unclipped pixels.")
    threshold, minimum = options["dark_cutoff"], options["min_pixels"]
    candidates = valid & (luminance(linear) <= threshold)
    groups, count = ndimage.label(candidates, structure=CONNECTIVITY)
    sizes = np.bincount(groups.ravel(), minlength=count + 1)
    # Padding is unavailable support even when the crop coincides with the working image edge.
    inside = ndimage.binary_erosion(valid, structure=CONNECTIVITY, border_value=0)
    opens = set(np.unique(groups[candidates & ~inside]).tolist())
    active = [label for label, number in enumerate(sizes) if label and number >= minimum]
    states = np.zeros(count + 1, dtype=np.uint8)
    totals = {"closed": 0, "truncated": 0}
    pixel_counts = {"closed": 0, "truncated": 0}
    for label in active:
        state = "truncated" if label in opens else "closed"
        states[label] = 2 if state == "truncated" else 1
        totals[state] += 1
        pixel_counts[state] += int(sizes[label])
    retained = sorted(active, key=lambda label: (-int(sizes[label]), label))[:MAX_RETAINED_FEATURES]
    rectangles = ndimage.find_objects(groups)
    rows = []
    for label in retained:
        ys, xs = rectangles[label - 1]
        y, x = np.nonzero(groups[ys, xs] == label)
        pixels = (y + ys.start, x + xs.start)
        kind = "truncated" if states[label] == 2 else "closed"
        rows.append(feature_record(kind, label, pixels, domain.shape, frame.scale))
    measurement = {
        "closed_count": totals["closed"],
        "truncated_count": totals["truncated"],
        "observed_pixels": observed,
        "unobserved_pixels": requested - observed,
        "closed_dark_percent": round(pixel_counts["closed"] * 100 / observed, 3),
        "truncated_dark_percent": round(pixel_counts["truncated"] * 100 / observed, 3),
        "features_omitted": len(active) - len(retained),
        "rows": rows,
        "note": "Dark candidates bounded by observed pixels are shown in green. Candidates with an unseen boundary are shown in amber. The measurement describes the visible section and does not establish void depth or distinguish every dark mineral.",
    }
    return measurement, observation_map(frame, domain, valid, states[groups])
