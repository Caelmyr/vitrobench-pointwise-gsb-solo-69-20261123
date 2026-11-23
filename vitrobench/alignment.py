"""Point-based pair alignment between two registered captures of one specimen.

A saved alignment re-places one capture (the second) into the shared specimen
plane so it matches another capture (the first, kept as the reference frame).
Two schemes are supported: one homography for the whole view, or two
homographies split at a marked divider with an unconfirmed middle band left
unmapped. Only one scheme is kept on the specimen at a time.
"""
import numpy as np

from .geometry import (
    DEFAULT_CORNERS,
    SampleFrame,
    homography,
    map_coordinates,
    specimen_frame,
    warp_capture,
    warp_capture_blocks,
)
from .model import (
    InputError,
    collection,
    dictionary,
    identity,
    numeric_pair,
    physical_scale,
    scalar,
    timestamp,
)
from .photos import open_working


MIN_PAIRS = 4
MAX_PAIRS = 40
MIN_SIDE_PAIRS = 4
SEAM_SAMPLES = 48
SEAM_MARGIN = 1.12
SEAM_SOURCE_FLOOR = 0.5
MAX_SEAM_FRACTION = 0.20


def alignment_request(payload):
    data = dictionary(payload, "Pair alignment")
    first = identity(data.get("first"), "First capture")
    second = identity(data.get("second"), "Second capture")
    if first == second:
        raise InputError("Choose two different captures for a pair alignment.")
    raw_pairs = collection(data.get("pairs"), "Corresponding points", minimum=MIN_PAIRS, maximum=MAX_PAIRS)
    pairs = []
    for item in raw_pairs:
        entry = dictionary(item, "Point pair")
        pairs.append({
            "first": numeric_pair(entry.get("first"), "First-view point", lower=0, upper=1),
            "second": numeric_pair(entry.get("second"), "Second-view point", lower=0, upper=1),
        })
    divider = None
    if data.get("divider") is not None:
        ends = collection(data.get("divider"), "Divider", minimum=2, maximum=2)
        divider = [numeric_pair(item, "Divider endpoint", lower=0, upper=1) for item in ends]
    tolerance = scalar(data.get("tolerance_px"), "Pairing error limit", lower=0.2, upper=60)
    return first, second, pairs, divider, tolerance


def registered_photos(tile):
    plane = tile.get("plane")
    if plane is None:
        raise InputError("Set a base photo and mark the tile plane first.")
    photos = {photo["id"]: photo for photo in tile["photos"]}
    registered = {plane["photo_id"], *tile["registrations"]}
    return plane, photos, registered


def check_pair(tile, first, second):
    plane, photos, registered = registered_photos(tile)
    for photo_id, label in ((first, "First capture"), (second, "Second capture")):
        if photo_id not in photos:
            raise InputError(f"{label} is no longer attached to this specimen.")
        if photo_id not in registered:
            raise InputError(f"Register the {label.lower()} in the specimen plane before aligning the pair.")
    return plane, photos


def normalizer(points):
    centroid = points.mean(axis=0)
    spread = float(np.mean(np.linalg.norm(points - centroid, axis=1)))
    scale = np.sqrt(2.0) / max(spread, 1e-12)
    return np.array([
        [scale, 0.0, -scale * centroid[0]],
        [0.0, scale, -scale * centroid[1]],
        [0.0, 0.0, 1.0],
    ])


def fit_homography(source, target, label):
    """Least-squares homography from normalized point correspondences."""
    source = np.asarray(source, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    if len(source) < MIN_PAIRS:
        raise InputError(f"{label} needs at least {MIN_PAIRS} point pairs.")
    if min(np.linalg.eigvalsh(np.cov(source.T))) < 1e-6:
        raise InputError(f"{label} points lie almost on one line. Spread them across the view.")
    to_source = normalizer(source)
    to_target = normalizer(target)
    src_n = apply_matrix(to_source, source)
    dst_n = apply_matrix(to_target, target)
    equations = []
    for (x, y), (u, v) in zip(src_n, dst_n):
        equations.append([-x, -y, -1.0, 0.0, 0.0, 0.0, x * u, y * u, u])
        equations.append([0.0, 0.0, 0.0, -x, -y, -1.0, x * v, y * v, v])
    _, _, vt = np.linalg.svd(np.asarray(equations))
    matrix = np.linalg.inv(to_target) @ vt[-1].reshape(3, 3) @ to_source
    if not np.all(np.isfinite(matrix)) or abs(matrix[2, 2]) < 1e-12:
        raise InputError(f"{label} points cannot define a stable alignment. Spread the points wider.")
    matrix /= matrix[2, 2]
    denominators = np.c_[source, np.ones(len(source))] @ matrix[2]
    if np.min(denominators) <= 0.05:
        raise InputError(f"{label} folds the view near the marked points. Check the point order.")
    return matrix


def apply_matrix(matrix, points):
    flat = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    mapped = np.c_[flat, np.ones(len(flat))] @ np.asarray(matrix, dtype=np.float64).T
    return (mapped[:, :2] / mapped[:, 2:3]).reshape(np.asarray(points).shape)


def pair_residuals(transform, pairs, size):
    first = np.asarray([pair["first"] for pair in pairs])
    second = np.asarray([pair["second"] for pair in pairs])
    mapped = apply_matrix(transform, first)
    return np.linalg.norm((mapped - second) * np.asarray(size, dtype=np.float64), axis=1)


def residual_report(values):
    values = np.asarray(values, dtype=np.float64)
    return {
        "rms_px": round(float(np.sqrt(np.mean(values**2))), 3),
        "max_px": round(float(np.max(values)), 3),
        "per_pair_px": [round(float(value), 3) for value in values],
    }


def corner_shift(transform_for_side, size, divider_info=None):
    corners = np.asarray(DEFAULT_CORNERS, dtype=np.float64)
    shifts = []
    for corner in corners:
        matrix = transform_for_side(corner) if divider_info else transform_for_side
        mapped = apply_matrix(matrix, corner)
        shifts.append(float(np.linalg.norm((mapped - corner) * np.asarray(size, dtype=np.float64))))
    return {"mean": round(float(np.mean(shifts)), 3), "max": round(float(np.max(shifts)), 3)}


def divider_model(divider, size):
    ends = np.asarray(divider, dtype=np.float64) * np.asarray(size, dtype=np.float64) - 0.5
    direction = ends[1] - ends[0]
    length = float(np.linalg.norm(direction))
    if length < 8:
        raise InputError("Mark a longer divider line across the view.")
    normal = np.array([-direction[1], direction[0]]) / length
    return ends[0], direction / length, normal, length


def signed_distance(points_unit, divider, size):
    origin, _, normal, _ = divider_model(divider, size)
    pixels = np.asarray(points_unit, dtype=np.float64) * np.asarray(size, dtype=np.float64) - 0.5
    return (pixels - origin) @ normal


def source_map(matrix, second_corners):
    return homography(DEFAULT_CORNERS, second_corners) @ matrix


def seam_report(m_left, m_right, divider, size, source_size, scale):
    origin, direction, normal, length = divider_model(divider, size)
    source_size = np.asarray(source_size, dtype=np.float64)
    size_array = np.asarray(size, dtype=np.float64)
    samples = origin[None, :] + np.linspace(0.04, 0.96, SEAM_SAMPLES)[:, None] * (length * direction)[None, :]
    unit = (samples + 0.5) / size_array
    step_unit = normal / size_array
    left_px = apply_matrix(m_left, unit) * source_size
    right_px = apply_matrix(m_right, unit) * source_size
    disagreement = np.linalg.norm(left_px - right_px, axis=1)
    left_step = np.linalg.norm(apply_matrix(m_left, unit + step_unit) * source_size - left_px, axis=1)
    right_step = np.linalg.norm(apply_matrix(m_right, unit + step_unit) * source_size - right_px, axis=1)
    local_scale = np.maximum((left_step + right_step) / 2, 1e-9)
    if float(np.max(disagreement)) < SEAM_SOURCE_FLOOR:
        half_width = 0.0
    else:
        half_width = float(np.max(disagreement / (2 * local_scale))) * SEAM_MARGIN + 0.5
    if half_width > MAX_SEAM_FRACTION * min(size):
        raise InputError(
            "The two sides disagree by too much across the divider for a credible split alignment."
        )
    gap = None
    if half_width > 0:
        left_edge = apply_matrix(m_left, unit - half_width * step_unit) * source_size
        right_edge = apply_matrix(m_right, unit + half_width * step_unit) * source_size
        gap = float(np.min(np.linalg.norm(right_edge - left_edge, axis=1)))
        if gap <= 0:
            raise InputError("The two sides would claim the same source area near the divider.")
    height, width = int(size[1]), int(size[0])
    yy, xx = np.mgrid[0:height, 0:width]
    distance = (xx - origin[0]) * normal[0] + (yy - origin[1]) * normal[1]
    unmapped = float(np.mean(np.abs(distance) <= half_width)) if half_width > 0 else 0.0
    return {
        "half_width_px": round(half_width, 2),
        "half_width_mm": round(half_width * (scale[0] + scale[1]) / 2, 3) if scale else None,
        "unmapped_percent": round(100 * unmapped, 2),
        "min_source_gap_px": round(gap, 3) if gap is not None else None,
    }


def scheme_single(pairs, size):
    first = [pair["first"] for pair in pairs]
    second = [pair["second"] for pair in pairs]
    transform = fit_homography(first, second, "A whole-view alignment")
    report = {
        "residuals": residual_report(pair_residuals(transform, pairs, size)),
        "corner_shift_px": corner_shift(transform, size),
        "seam": None,
        "sides": None,
    }
    return report, {"single": transform}


def scheme_split(pairs, divider, size, scale, source_size, second_corners):
    if divider is None:
        raise InputError("Mark the two divider endpoints to evaluate the two-sided scheme.")
    distance = signed_distance([pair["first"] for pair in pairs], divider, size)
    sides = {}
    for name, wanted in (("left", -1), ("right", 1)):
        chosen = [pair for pair, side in zip(pairs, distance) if np.sign(side) == wanted or side == 0 and wanted < 0]
        if len(chosen) < MIN_SIDE_PAIRS:
            raise InputError(
                f"The {name} side of the divider holds {len(chosen)} pairs; each side needs at least {MIN_SIDE_PAIRS}."
            )
        transform = fit_homography(
            [pair["first"] for pair in chosen],
            [pair["second"] for pair in chosen],
            f"The {name} side",
        )
        sides[name] = {"transform": transform, "pairs": chosen, "count": len(chosen)}
    origin, _, normal, _ = divider_model(divider, size)

    def transform_for_side(corner):
        side = (np.asarray(corner) * np.asarray(size) - 0.5 - origin) @ normal
        return sides["left" if side < 0 else "right"]["transform"]

    residuals = np.concatenate([
        pair_residuals(sides[name]["transform"], sides[name]["pairs"], size) for name in ("left", "right")
    ])
    m_left = source_map(sides["left"]["transform"], second_corners)
    m_right = source_map(sides["right"]["transform"], second_corners)
    seam = seam_report(m_left, m_right, divider, size, source_size, scale)
    report = {
        "residuals": residual_report(residuals),
        "corner_shift_px": corner_shift(transform_for_side, size, divider_info=True),
        "seam": seam,
        "sides": {
            name: {
                "pairs": sides[name]["count"],
                "rms_px": residual_report(pair_residuals(sides[name]["transform"], sides[name]["pairs"], size))["rms_px"],
            }
            for name in ("left", "right")
        },
    }
    return report, {"left": sides["left"]["transform"], "right": sides["right"]["transform"]}


def scheme_note(kind, report):
    residuals = report["residuals"]
    if kind == "single":
        return (
            "One transform covers the whole view, so every position keeps a correspondence and there is no "
            f"seam. The outer frame moves at most {report['corner_shift_px']['max']:.2f} px from the current "
            f"corner registration; the marked details fit to {residuals['rms_px']:.2f} px RMS "
            f"(worst pair {residuals['max_px']:.2f} px)."
        )
    sides = report["sides"]
    seam = report["seam"]
    width = f"±{seam['half_width_px']:.1f} px"
    if seam["half_width_mm"] is not None:
        width += f" (±{seam['half_width_mm']:.2f} mm)"
    return (
        f"Each side fits its own transform, so painted details align tighter (left {sides['left']['rms_px']:.2f} px, "
        f"right {sides['right']['rms_px']:.2f} px RMS). A middle band {width} along the divider "
        f"({seam['unmapped_percent']:.1f}% of the view) has no confirmed correspondence and is left empty."
    )


def evaluate_schemes(tile, first, second, pairs, divider, tolerance):
    plane, photos = check_pair(tile, first, second)
    size = tuple(plane["size"])
    second_photo = photos[second]
    corners = plane["corners"] if second == plane["photo_id"] else tile["registrations"][second]["corners"]
    source_size = (second_photo["width"], second_photo["height"])
    scale = physical_scale(tile, *size)
    schemes = {}
    builders = {
        "single": lambda: scheme_single(pairs, size),
        "split": lambda: scheme_split(pairs, divider, size, scale, source_size, corners),
    }
    for name, build in builders.items():
        try:
            report, transforms = build()
        except InputError as error:
            schemes[name] = {"feasible": False, "reason": str(error)}
            continue
        report["feasible"] = True
        report["meets_tolerance"] = report["residuals"]["max_px"] <= tolerance
        report["note"] = scheme_note(name, report)
        report["transforms"] = transforms
        schemes[name] = report
    return {
        "first_photo_id": first,
        "second_photo_id": second,
        "tolerance_px": tolerance,
        "plane_size": list(size),
        "schemes": schemes,
    }


def public_report(evaluation):
    schemes = {}
    for name, report in evaluation["schemes"].items():
        if not report.get("feasible"):
            schemes[name] = {"feasible": False, "reason": report["reason"]}
        else:
            schemes[name] = {
                key: report[key]
                for key in ("feasible", "meets_tolerance", "residuals", "corner_shift_px", "seam", "sides", "note")
            }
    return {**evaluation, "schemes": schemes}


def build_alignment(tile, first, second, pairs, divider, tolerance, mode):
    evaluation = evaluate_schemes(tile, first, second, pairs, divider, tolerance)
    scheme = evaluation["schemes"][mode]
    if not scheme.get("feasible"):
        raise InputError(scheme["reason"])
    if not scheme["meets_tolerance"]:
        worst = scheme["residuals"]["max_px"]
        raise InputError(
            f"The kept scheme places a marked pair {worst:.2f} px off, above the {tolerance:.2f} px limit. "
            "Adjust the points or raise the limit."
        )
    plane, photos = check_pair(tile, first, second)
    second_photo = photos[second]
    corners = plane["corners"] if second == plane["photo_id"] else tile["registrations"][second]["corners"]
    matrices = {
        name: [round(float(value), 10) for value in source_map(transform, corners).ravel()]
        for name, transform in scheme["transforms"].items()
    }
    return {
        "first_photo_id": first,
        "second_photo_id": second,
        "mode": mode,
        "tolerance_px": tolerance,
        "pairs": pairs,
        "divider": divider,
        "matrices": matrices,
        "residuals": scheme["residuals"],
        "corner_shift_px": scheme["corner_shift_px"],
        "seam": scheme["seam"],
        "created_at": timestamp(),
    }


def alignment_summary(record):
    return {
        "mode": record["mode"],
        "tolerance_px": record["tolerance_px"],
        "residuals": {key: record["residuals"][key] for key in ("rms_px", "max_px")},
        "seam": record["seam"],
    }


def alignment_matrices(record):
    return {name: np.asarray(values, dtype=np.float64).reshape(3, 3) for name, values in record["matrices"].items()}


def aligned_second_frame(blobs, tile, record=None):
    record = record or tile.get("pair_alignment")
    if record is None:
        raise InputError("This specimen has no saved pair alignment.")
    plane, photos, _ = registered_photos(tile)
    second = record["second_photo_id"]
    if second not in photos:
        raise InputError("The aligned capture was removed from this specimen.")
    photo = photos[second]
    destination = tuple(plane["size"])
    rgb, alpha = open_working(blobs, photo)
    matrices = alignment_matrices(record)
    if "single" in matrices:
        matrix = matrices["single"]
        linear, valid, clipped = warp_capture(rgb, alpha, destination, matrix)
        transform = tuple(matrix.ravel())
    else:
        divider = record["divider"]
        half_width = record["seam"]["half_width_px"]
        origin, _, normal, _ = divider_model(divider, destination)

        def coordinates(start, stop, extent, source_extent):
            width, height = extent
            target_x = (np.arange(width, dtype=np.float32)[None, :] + 0.5) / width
            target_y = (np.arange(start, stop, dtype=np.float32)[:, None] + 0.5) / height
            px_x = target_x * width - 0.5
            px_y = target_y * height - 0.5
            distance = (px_x - origin[0]) * normal[0] + (px_y - origin[1]) * normal[1]
            left_x, left_y = map_coordinates(matrices["left"], target_x, target_y, *source_extent)
            right_x, right_y = map_coordinates(matrices["right"], target_x, target_y, *source_extent)
            use_left = distance < -half_width
            use_right = distance > half_width
            src_x = np.where(use_left, left_x, right_x)
            src_y = np.where(use_left, left_y, right_y)
            return src_x, src_y, use_left | use_right

        linear, valid, clipped = warp_capture_blocks(rgb, alpha, destination, coordinates)
        transform = tuple(matrices["left"].ravel()) + tuple(matrices["right"].ravel())
    if np.count_nonzero(valid) < 64:
        raise InputError("The saved alignment leaves too few usable pixels in the specimen plane.")
    return SampleFrame(linear, valid, photo, transform, physical_scale(tile, *destination), clipped)


def paired_view(store, tile, photo_id):
    """The other capture of the saved alignment, framed for a line scan.

    Returns (frame, position_mapper, record). The mapper converts plane
    positions of the measured capture into plane positions of the paired
    frame; it is None when both already share the same plane positions.
    """
    record = tile.get("pair_alignment")
    if record is None:
        return None
    if photo_id == record["first_photo_id"]:
        return aligned_second_frame(store.blobs, tile, record), None, record
    if photo_id != record["second_photo_id"]:
        return None
    frame = specimen_frame(store.blobs, tile, record["first_photo_id"])
    plane, photos, _ = registered_photos(tile)
    second = photos[record["second_photo_id"]]
    corners = plane["corners"] if second["id"] == plane["photo_id"] else tile["registrations"][second["id"]]["corners"]
    to_source = homography(DEFAULT_CORNERS, corners)
    matrices = alignment_matrices(record)
    size = tuple(plane["size"])

    def mapper(points_unit):
        source = apply_matrix(to_source, points_unit)
        if "single" in matrices:
            return apply_matrix(np.linalg.inv(matrices["single"]), source), np.ones(len(source), dtype=bool)
        half_width = record["seam"]["half_width_px"]
        left = apply_matrix(np.linalg.inv(matrices["left"]), source)
        right = apply_matrix(np.linalg.inv(matrices["right"]), source)
        on_left = signed_distance(left, record["divider"], size) < -half_width
        on_right = signed_distance(right, record["divider"], size) > half_width
        mapped = np.where(on_left[:, None], left, right)
        return mapped, on_left | on_right

    return frame, mapper, record
