import numpy as np
from scipy import ndimage

from .art import composite_color
from .color import linear_lab
from .coverage import reference_region, sample_chip
from .geometry import normalized_bbox, region_domain


BLOOM_COLOR = (247, 183, 87)
CONTACT_COLOR = (107, 173, 223)
CONNECTIVITY = np.ones((3, 3), dtype=bool)
MAX_REPORTED_COMPONENTS = 200


def label_bloom_candidates(lab, control, support, options):
    chroma = np.hypot(lab[:, :, 1], lab[:, :, 2])
    lighter = lab[:, :, 0] - control[0] >= options["minimum_lightness_gain"]
    neutral = chroma <= options["maximum_chroma"]
    labels, _ = ndimage.label(support & lighter & neutral, structure=CONNECTIVITY)
    counts = np.bincount(labels.ravel())
    retained = counts >= options["minimum_pixels"]
    retained[0] = False
    return labels, counts, retained


def component_rows(frame, labels, counts, retained, support):
    contact = ndimage.binary_dilation(~support, structure=CONNECTIVITY)
    contact[[0, -1], :] = True
    contact[:, [0, -1]] = True
    touches_edge = np.zeros(len(counts), dtype=bool)
    touches_edge[labels[contact]] = True
    ids = sorted(np.flatnonzero(retained), key=lambda item: (-counts[item], item))
    slices = ndimage.find_objects(labels)
    scale = frame.scale
    rows = []
    for item in ids[:MAX_REPORTED_COMPONENTS]:
        bounds = slices[item - 1]
        pixels = int(counts[item])
        rows.append({
            "id": f"B{item}",
            "bounds_normalized": [round(value, 5) for value in normalized_bbox(bounds, frame.extent)],
            "pixels": pixels,
            "square_mm": round(pixels * scale[0] * scale[1], 4) if scale else None,
            "state": "Touches support boundary" if touches_edge[item] else "Within support",
        })
    return rows, touches_edge, len(ids)


def measure_bloom(frame, tile, area, support, linear, options):
    lab = linear_lab(linear)
    control_region = reference_region(tile, "glazed", options["pigment_region"])
    control, _, reference = sample_chip(frame, lab, region_domain(frame.extent, tile), control_region)
    labels, counts, retained = label_bloom_candidates(lab, control, support, options)
    rows, touches_edge, components = component_rows(frame, labels, counts, retained, support)
    candidate_mask = retained[labels]
    observed = int(np.count_nonzero(support))
    candidate_pixels = int(np.count_nonzero(candidate_mask))
    metrics = {
        "control": reference,
        "component_count": components,
        "boundary_count": int(np.count_nonzero(retained & touches_edge)),
        "observed_pixels": observed,
        "unobserved_pixels": int(np.count_nonzero(area & ~support)),
        "candidate_pixels": candidate_pixels,
        "candidate_percent": round(100 * candidate_pixels / observed, 3),
        "rows_omitted": components - len(rows),
        "rows": rows,
        "note": "Pale deposits can resemble each other in photographs. Inspect mapped candidates before sampling.",
    }
    palette = ((0, BLOOM_COLOR), (1, CONTACT_COLOR))
    overlay = composite_color(
        frame, touches_edge[labels].astype(float), candidate_mask, palette,
        source=linear, unknown=area & ~support,
    )
    return metrics, overlay
