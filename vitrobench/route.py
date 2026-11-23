"""Plan a protective-strip route along photographed cracks, then audit saved observations against it."""

from dataclasses import dataclass
import itertools
import math

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from .art import SHADOW, base_rgba, stored_preview
from .color import corrected_linear
from .craze import crack_centerline
from .geometry import specimen_frame
from .model import (
    ConflictError,
    InputError,
    MissingError,
    collection,
    dictionary,
    enumeration,
    flag,
    integer,
    numeric_pair,
    polyline,
    unique_ids,
)
from .parameters import ROUTE_FIELDS, parsed_option


NEIGHBOURS = (
    (-1, -1, 1.4142135623730951), (-1, 0, 1.0), (-1, 1, 1.4142135623730951),
    (0, -1, 1.0), (0, 1, 1.0),
    (1, -1, 1.4142135623730951), (1, 0, 1.0), (1, 1, 1.4142135623730951),
)
PREFERENCES = ("balanced", "evidence", "edge")
PRIORITY_WEIGHTS = {"balanced": (0.5, 0.5), "evidence": (0.78, 0.22), "edge": (0.22, 0.78)}
CANDIDATE_STYLES = {
    "evidence": {"off": 26.0, "dual": 0.45, "single": 1.55, "paint": 2.4, "crack": 0.55, "edge": 0.12},
    "edge": {"off": 11.0, "dual": 0.62, "single": 1.32, "paint": 2.4, "crack": 0.85, "edge": 0.9},
}
MAX_CRACK_SEGMENTS = 6
MAX_PAINTED_LINES = 8
MAX_EDGE_LINES = 4
MAX_RECORDED_PHOTOS = 10
MIN_CRACK_COVERAGE = 0.6
CRACK_TOLERANCE_PX = 2.5
GATE_REACH_PX = 8.0
MIN_DUAL_VISIBILITY = 0.35
CANDIDATE_NAMES = ("A", "B")
CANDIDATE_COLORS = {"A": (97, 214, 236), "B": (246, 166, 104)}
ADOPTED_COLOR = (247, 202, 96)
DUAL_COLOR = (126, 234, 165)
PAINT_COLOR = (176, 181, 189)
EDGE_COLOR = (120, 186, 244)
BAND_COLOR = (251, 235, 188)
UNCOVERED_COLOR = (240, 118, 110)
OUTLINE_COLOR = (250, 246, 226)
AUDIT_SCALE = 4
MAX_AUDIT_PARTS = 8


@dataclass(frozen=True)
class Band:
    left: int
    top: int
    shape: tuple
    along: np.ndarray
    distance: np.ndarray
    start: np.ndarray
    finish: np.ndarray
    length_px: float

    @property
    def mask(self):
        return self.distance >= 0


@dataclass(frozen=True)
class Evidence:
    traces: dict
    union: np.ndarray
    count: np.ndarray
    photographed: np.ndarray
    skipped: list
    thresholds: dict
    roles: dict


@dataclass(frozen=True)
class Context:
    frame: object
    band: Band
    options: dict
    painted: np.ndarray
    edge_distance: object
    edge_points: object
    cracks: list
    evidence: Evidence
    support: np.ndarray


def route_options(payload):
    raw = dictionary(payload.get("options", {}), "Tape-route options")
    values = {
        key: parsed_option(key, field, raw.get(key, field["default"]))
        for key, field in ROUTE_FIELDS.items()
    }
    values["trace"] = {
        "radius": 1.4,
        "sensitivity": values["line_sensitivity"],
        "join_px": 1,
        "component_minimum": 12,
        "minimum_cell": 24,
    }
    return values


def visibility_answers(payload):
    entries = collection(payload.get("crack_visibility", []), "Crack visibility answers", maximum=MAX_CRACK_SEGMENTS)
    answers = {}
    for entry in entries:
        item = dictionary(entry, "Crack visibility answer")
        index = integer(item.get("index"), "Crack index", lower=1, upper=MAX_CRACK_SEGMENTS)
        if index in answers:
            raise InputError("Each confirmed short crack can be answered once.")
        answers[index] = flag(item.get("visible"), "Crack visible in captures")
    return answers


def read_route_payload(payload):
    data = dictionary(payload, "Tape route")
    band = dictionary(data.get("band"), "Inspection band")
    start = numeric_pair(band.get("start"), "Band start", lower=0, upper=1)
    end = numeric_pair(band.get("end"), "Band end", lower=0, upper=1)
    if start == end:
        raise InputError("Mark two different ends for the inspection band.")
    cracks = [polyline(item, "Confirmed short crack", minimum=2, maximum=60)
              for item in collection(data.get("confirmed_cracks", []), "Confirmed short cracks",
                                     maximum=MAX_CRACK_SEGMENTS)]
    painted = [polyline(item, "Painted line", minimum=2, maximum=90)
               for item in collection(data.get("painted_lines", []), "Painted lines",
                                      maximum=MAX_PAINTED_LINES)]
    edges = [polyline(item, "Fragile edge", minimum=2, maximum=90)
             for item in collection(data.get("fragile_edges", []), "Fragile edges",
                                    maximum=MAX_EDGE_LINES)]
    return {
        "band": {"start": start, "end": end},
        "confirmed_cracks": cracks,
        "painted_lines": painted,
        "fragile_edges": edges,
        "preference": enumeration(data.get("preference", "balanced"), PREFERENCES, "Route preference"),
        "photo_ids": unique_ids(data.get("photo_ids", []), "Route captures", maximum=MAX_RECORDED_PHOTOS),
        "visibility": visibility_answers(data),
        "options": route_options(data),
    }


def plane_frame(store, tile):
    if not tile.get("plane"):
        raise InputError("Set a base photograph before planning a tape route.")
    return specimen_frame(store.blobs, tile, tile["plane"]["photo_id"])


def band_geometry(frame, band, width_px):
    width, height = frame.extent
    start = np.asarray(band["start"], dtype=np.float64) * [width, height]
    finish = np.asarray(band["end"], dtype=np.float64) * [width, height]
    vector = finish - start
    length = float(np.linalg.norm(vector))
    if length < 24:
        raise InputError("Mark an inspection band at least 24 registered pixels long.")
    reach = width_px / 2 + 4
    left = max(0, int(np.floor(min(start[0], finish[0]) - reach)))
    right = min(width, int(np.ceil(max(start[0], finish[0]) + reach)) + 1)
    top = max(0, int(np.floor(min(start[1], finish[1]) - reach)))
    bottom = min(height, int(np.ceil(max(start[1], finish[1]) + reach)) + 1)
    if right - left < 3 or bottom - top < 3:
        raise InputError("The inspection band lies outside the registered specimen plane.")
    yy, xx = np.mgrid[top:bottom, left:right]
    xx = xx + 0.5
    yy = yy + 0.5
    direction = vector / length
    relative_x = xx - start[0]
    relative_y = yy - start[1]
    along = relative_x * direction[0] + relative_y * direction[1]
    perpendicular = np.abs(relative_x * direction[1] - relative_y * direction[0])
    beyond = np.maximum(-along, along - length)
    distance = np.where((along >= 0) & (along <= length), perpendicular, np.hypot(beyond, perpendicular))
    distance = np.where(distance <= width_px / 2, distance, -1.0)
    return Band(left, top, (bottom - top, right - left), along, distance, start, finish, length)


def crop(window, array):
    top, left, shape = window.top, window.left, window.shape
    return array[top:top + shape[0], left:left + shape[1]]


def draw_polylines(window, polylines, width_px, extent):
    canvas = Image.new("L", (window.shape[1], window.shape[0]), 0)
    artist = ImageDraw.Draw(canvas)
    for points in polylines:
        values = np.asarray(points, dtype=np.float64) * [extent[0], extent[1]]
        values = values - [window.left, window.top]
        coordinates = [(float(x), float(y)) for x, y in values]
        if len(coordinates) >= 2:
            artist.line(coordinates, fill=255, width=max(1, int(round(width_px))), joint="curve")
        elif coordinates:
            x, y = coordinates[0]
            radius = max(1.0, width_px / 2)
            artist.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
    return np.asarray(canvas, dtype=bool) & window.mask


def sample_polyline(points, extent, step=0.5):
    values = np.asarray(points, dtype=np.float64) * [extent[0], extent[1]]
    if len(values) < 2:
        return values.reshape(-1, 2), np.zeros(0)
    pieces = [values[:1]]
    spacing = []
    for first, second in zip(values[:-1], values[1:]):
        span = float(np.linalg.norm(second - first))
        count = max(2, int(math.ceil(span / step)) + 1)
        fractions = np.linspace(0, 1, count)[1:]
        piece = first[None, :] + fractions[:, None] * (second - first)[None, :]
        pieces.append(piece)
        spacing.extend([span / (count - 1)] * (count - 1))
    return np.concatenate(pieces, axis=0), np.asarray(spacing, dtype=np.float64)


def grid_positions(coords, window):
    local = np.asarray(coords, dtype=np.float64) - [window.left, window.top]
    rows = np.clip(np.floor(local[:, 1]).astype(np.int64), 0, window.shape[0] - 1)
    cols = np.clip(np.floor(local[:, 0]).astype(np.int64), 0, window.shape[1] - 1)
    inside = (
        (local[:, 0] >= 0) & (local[:, 0] < window.shape[1])
        & (local[:, 1] >= 0) & (local[:, 1] < window.shape[0])
    )
    return rows, cols, inside


def trace_window(frame, tile, window, options):
    # Trace on a region dilated past the band so the detector's smoothing support is not
    # truncated at the band edge, then keep only the evidence inside the band.
    margin = int(math.ceil(options["trace"]["radius"] * 8)) + 6
    region = np.zeros(frame.valid.shape, dtype=bool)
    region[window.top:window.top + window.shape[0], window.left:window.left + window.shape[1]] = window.mask
    region = ndimage.binary_dilation(region, iterations=margin)
    linear, _ = corrected_linear(tile, frame, options["apply_neutral"])
    thin, support, _, grades = crack_centerline(frame, linear, region, options["trace"])
    return crop(window, thin & support), grades


def collect_evidence(store, tile, frame, window, options, requested):
    plane = tile["plane"]
    registered = {plane["photo_id"], *tile["registrations"]}
    photos = [record for record in tile["photos"] if record["id"] in registered]
    if requested:
        chosen = set(unique_ids(requested, "Route captures", maximum=MAX_RECORDED_PHOTOS))
        if chosen - registered:
            raise InputError("Every chosen route capture must be registered in the specimen plane.")
        photos = [record for record in photos if record["id"] in chosen]
    if not photos:
        raise InputError("Register at least one capture before planning a tape route.")
    traces = {}
    skipped = []
    thresholds = {}
    roles = {}
    photographed = np.zeros(window.shape, dtype=bool)
    for record in photos:
        capture = frame if record["id"] == plane["photo_id"] else specimen_frame(store.blobs, tile, record["id"])
        available = crop(window, capture.valid)
        if np.count_nonzero(available & window.mask) < 60:
            skipped.append({"photo": record["id"], "role": record["role"],
                            "reason": "too little photographed support inside the band"})
            continue
        try:
            thin, grades = trace_window(capture, tile, window, options)
        except InputError as error:
            skipped.append({"photo": record["id"], "role": record["role"], "reason": str(error)})
            continue
        if np.count_nonzero(thin) < 6:
            skipped.append({"photo": record["id"], "role": record["role"],
                            "reason": "no fine linework traced inside the band"})
            continue
        traces[record["id"]] = thin
        thresholds[record["id"]] = grades
        roles[record["id"]] = record["role"]
        photographed |= available
    if not traces:
        raise InputError("No registered capture supplied usable fine-line evidence inside this band.")
    stack = np.stack([ndimage.binary_dilation(item, iterations=1) for item in traces.values()]).astype(np.uint8)
    count = stack.sum(axis=0).astype(np.uint8)
    return Evidence(traces=traces, union=count >= 1, count=count, photographed=photographed,
                    skipped=skipped, thresholds=thresholds, roles=roles)


def crack_coverage(context, path_points):
    """Fraction of each confirmed crack's sampled length within tolerance of the route."""
    result = []
    if not len(path_points):
        for entry in context.cracks:
            result.append({"index": entry["index"], "covered_fraction": 0.0, "can_anchor": False})
        return result
    tree = cKDTree(np.asarray(path_points, dtype=np.float64))
    for entry in context.cracks:
        coords, _ = sample_polyline(entry["points"], context.frame.extent)
        distances = tree.query(coords)[0] if len(coords) else np.zeros(0)
        fraction = float(np.mean(distances <= CRACK_TOLERANCE_PX)) if len(distances) else 0.0
        result.append({
            "index": entry["index"],
            "covered_fraction": round(fraction, 4),
            "can_anchor": bool(entry["visible"]),
        })
    return result


def build_context(store, tile, record):
    options = record["options"]
    frame = plane_frame(store, tile)
    window = band_geometry(frame, record["band"], options["band_width"])
    evidence = collect_evidence(store, tile, frame, window, options, record["photo_ids"])
    tolerance = int(round(options["support_tolerance"]))
    support = ndimage.binary_dilation(evidence.photographed, iterations=tolerance) & window.mask
    painted = draw_polylines(window, record["painted_lines"], options["painted_width"], frame.extent)
    edge_distance = None
    edge_points = np.zeros((0, 2))
    if record["fragile_edges"]:
        edge_mask = draw_polylines(window, record["fragile_edges"], 1, frame.extent)
        edge_distance = ndimage.distance_transform_edt(~edge_mask)
        collected = []
        for points in record["fragile_edges"]:
            coords, _ = sample_polyline(points, frame.extent)
            rows, cols, inside = grid_positions(coords, window)
            near = inside & (window.distance[rows, cols] >= -options["edge_contact"])
            if np.any(near):
                collected.append(coords[near])
        if collected:
            edge_points = np.concatenate(collected, axis=0)
    cracks = []
    reach = ndimage.binary_dilation(evidence.union, iterations=2)
    for index, points in enumerate(record["confirmed_cracks"], start=1):
        coords, _ = sample_polyline(points, frame.extent)
        rows, cols, inside = grid_positions(coords, window)
        detected = float(np.mean(reach[rows[inside], cols[inside]])) >= MIN_DUAL_VISIBILITY if np.any(inside) else False
        answers = record["visibility"].get(index)
        if answers is None and not detected:
            raise InputError(
                f"Confirmed short crack {index} is not visible in any registered capture. "
                "Confirm that it is absent, or mark it visible, before planning a route."
            )
        if answers is True and not detected:
            raise InputError(
                f"Confirmed short crack {index} is not visible in any registered capture, so no route may follow it."
            )
        if answers is False and detected:
            raise InputError(
                f"Confirmed short crack {index} is traced in the captures. Clear the 'not visible' answer or remove the crack."
            )
        cracks.append({
            "index": index,
            "points": points,
            "detected_in_captures": bool(detected),
            "declared_visible": answers,
            "visible": bool(detected if answers is None else answers),
        })
    return Context(frame=frame, band=window, options=options, painted=painted,
                   edge_distance=edge_distance, edge_points=edge_points, cracks=cracks,
                   evidence=evidence, support=support)


class Grid:
    def __init__(self, context):
        window = context.band
        self.window = window
        self.traversable = window.mask & context.support
        if np.count_nonzero(self.traversable) < 64:
            raise InputError("The band holds too little photographed glass to plan a tape route.")
        rows, cols = np.nonzero(self.traversable)
        self.rows = rows
        self.cols = cols
        self.lookup = np.full(window.shape, -1, dtype=np.int64)
        self.lookup[rows, cols] = np.arange(len(rows))
        self.points = np.stack((cols + 0.5, rows + 0.5), axis=1)
        self.tree = cKDTree(self.points)
        self.adjacency = self._adjacency()
        self.dual = context.evidence.count[rows, cols] >= 2
        self.single = (context.evidence.count[rows, cols] == 1) & ~self.dual
        self.painted = context.painted[rows, cols]
        self.photographed = context.evidence.photographed[rows, cols]
        self.crack = np.zeros(len(rows), dtype=bool)
        for entry in context.cracks:
            if not entry["visible"]:
                continue
            coords, _ = sample_polyline(entry["points"], context.frame.extent)
            local = coords - [window.left, window.top]
            keep = (
                (local[:, 0] >= 0) & (local[:, 0] < window.shape[1])
                & (local[:, 1] >= 0) & (local[:, 1] < window.shape[0])
            )
            if not np.any(keep):
                continue
            near = np.round(local[keep][:, ::-1] - 0.5).astype(np.int64)
            near[:, 0] = np.clip(near[:, 0], 0, window.shape[0] - 1)
            near[:, 1] = np.clip(near[:, 1], 0, window.shape[1] - 1)
            nodes = self.lookup[near[:, 0], near[:, 1]]
            self.crack[nodes[nodes >= 0]] = True
        self.edge_proximity = np.zeros(len(rows))
        if context.edge_distance is not None:
            self.edge_proximity = np.exp(-context.edge_distance[rows, cols] / context.options["edge_contact"])

    def _adjacency(self):
        height, width = self.window.shape
        links = []
        lookup = self.lookup
        for row, col in zip(self.rows.tolist(), self.cols.tolist()):
            entries = []
            for dy, dx, step in NEIGHBOURS:
                other_row = row + dy
                other_col = col + dx
                if 0 <= other_row < height and 0 <= other_col < width:
                    other = int(lookup[other_row, other_col])
                    if other >= 0:
                        entries.append((other, step))
            links.append(entries)
        return links

    def nearest(self, coordinate, distance):
        span, index = self.tree.query(np.asarray(coordinate, dtype=np.float64), k=1)
        if not np.isfinite(span) or span > distance:
            return None
        return int(index)

    def matrix(self, cost):
        lines = []
        columns = []
        values = []
        for node, links in enumerate(self.adjacency):
            for other, step in links:
                lines.append(node)
                columns.append(other)
                values.append(step * cost[other])
        return csr_matrix((values, (lines, columns)), shape=(len(self.adjacency),) * 2)


def candidate_cost(context, grid, style):
    cost = np.full(len(grid.rows), style["off"], dtype=np.float64)
    cost[grid.single] = style["single"]
    cost[grid.dual] = style["dual"]
    if context.edge_distance is not None:
        cost = cost * (1 - style["edge"] * grid.edge_proximity)
    cost[grid.crack] *= style["crack"]
    cost[grid.painted] = style["paint"]
    return np.maximum(cost, 0.04)


def gate_nodes(grid, context):
    start_window = context.band.start - [context.band.left, context.band.top]
    finish_window = context.band.finish - [context.band.left, context.band.top]
    tolerance = context.options["endpoint_tolerance"]
    start = grid.nearest(start_window, tolerance)
    finish = grid.nearest(finish_window, tolerance)
    if start is None:
        raise InputError("The first band end has no photographed crack evidence within its reach.")
    if finish is None:
        raise InputError("The second band end has no photographed crack evidence within its reach.")
    gates = [start]
    for entry in context.cracks:
        if not entry["visible"]:
            continue
        coords, _ = sample_polyline(entry["points"], context.frame.extent)
        local = coords - [context.band.left, context.band.top]
        gate = None
        for fraction in (0.5, 0.0, 1.0):
            point = local[int(round(fraction * (len(local) - 1)))] if len(local) else None
            if point is None:
                continue
            gate = grid.nearest(point, GATE_REACH_PX)
            if gate is not None:
                break
        if gate is None:
            raise InputError(
                f"Confirmed short crack {entry['index']} has no photographed line evidence to anchor the route."
            )
        gates.append(gate)
    return start, finish, gates


def shortest_legs(graph, sources):
    return dijkstra(graph, directed=True, indices=sources, return_predecessors=True)


def walk(predecessors, row, source_node, target):
    if target == source_node:
        return [source_node]
    path = [target]
    guard = 0
    while path[-1] != source_node:
        step = int(predecessors[row, path[-1]])
        if step < 0 or guard > 2_000_000:
            return None
        path.append(step)
        guard += 1
    path.reverse()
    return path


def order_gates(graph, start, finish, gates):
    required = []
    seen = set()
    for gate in gates:
        if gate in (start, finish) or gate in seen:
            continue
        seen.add(gate)
        required.append(gate)
    sources = [start, *required]
    distances, predecessors = shortest_legs(graph, sources)
    best = None
    for order in itertools.permutations(range(len(required))):
        total = 0.0
        cursor = 0
        for target in order:
            leg = distances[cursor, required[target]]
            if not np.isfinite(leg):
                total = math.inf
                break
            total += leg
            cursor = target + 1
        if not math.isfinite(total):
            continue
        last = distances[cursor, finish]
        if not np.isfinite(last):
            continue
        total += last
        if best is None or total < best[0]:
            best = (total, order)
    if best is None:
        return None
    _, order = best
    path = []
    cursor_row = 0
    cursor_node = start
    for target in order:
        leg = walk(predecessors, cursor_row, cursor_node, required[target])
        if leg is None:
            return None
        path.extend(leg if not path else leg[1:])
        cursor_row = target + 1
        cursor_node = required[target]
    leg = walk(predecessors, cursor_row, cursor_node, finish)
    if leg is None:
        return None
    path.extend(leg if not path else leg[1:])
    return path


def node_global(grid, index):
    return np.asarray([grid.cols[index] + 0.5, grid.rows[index] + 0.5], dtype=np.float64)


def global_path(grid, nodes, window):
    return [node_global(grid, index) + [window.left, window.top] for index in nodes]


def simplify_points(points, tolerance=0.9):
    values = np.asarray(points, dtype=np.float64)
    if len(values) < 3:
        return values.tolist()
    keep = [0]
    stack = [(0, len(values) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        start = values[first]
        finish = values[last]
        delta = finish - start
        span = np.linalg.norm(delta)
        section = values[first + 1:last]
        if span < 1e-9:
            gaps = np.linalg.norm(section - start, axis=1)
        else:
            gaps = np.abs(delta[0] * (section[:, 1] - start[1]) - delta[1] * (section[:, 0] - start[0])) / span
        position = int(np.argmax(gaps)) + first + 1
        if gaps.max() > tolerance:
            keep.append(position)
            stack.append((first, position))
            stack.append((position, last))
    keep = sorted(set(keep + [len(values) - 1]))
    return [values[index].tolist() for index in keep]


def stretch_flags(mask, steps):
    flags = np.asarray(mask, dtype=bool)
    parts = []
    position = 0
    while position < len(flags):
        if not flags[position]:
            position += 1
            continue
        start = position
        while position < len(flags) and flags[position]:
            position += 1
        parts.append((start, position, float(np.sum(steps[start:position]))))
    return parts


def path_metrics(context, grid, nodes):
    window = context.band
    frame = context.frame
    factor = float(frame.scale[0] * frame.scale[1]) ** 0.5 if frame.scale else 1.0
    unit = "mm" if frame.scale else "px"
    points = np.asarray(global_path(grid, nodes, window), dtype=np.float64)
    steps = np.linalg.norm(np.diff(points, axis=0), axis=1) if len(points) > 1 else np.zeros(0)
    if not len(steps):
        raise InputError("The planned route collapsed to a single point.")
    total = float(np.sum(steps))
    dual = grid.dual[nodes[:-1]]
    single = grid.single[nodes[:-1]]
    painted = grid.painted[nodes[:-1]]
    photographed = grid.photographed[nodes[:-1]]
    evidence = dual | single
    off = ~evidence & ~painted
    dual_length = float(np.sum(steps[dual]))
    single_length = float(np.sum(steps[single]))
    off_length = float(np.sum(steps[off]))
    painted_parts = stretch_flags(painted, steps)
    crossing_limit = context.options["crossing_gap"]
    crossings = [part for part in painted_parts if part[2] <= crossing_limit]
    follows = [part for part in painted_parts if part[2] > crossing_limit]
    unsupported_parts = stretch_flags(~photographed, steps)
    bridge_limit = context.options["support_tolerance"]
    bridges = [part for part in unsupported_parts if part[2] > bridge_limit]
    off_parts = stretch_flags(off, steps)
    tolerance = max(context.options["support_tolerance"], context.options["crossing_gap"] / 2)
    endpoint_parts = []
    span = context.options["endpoint_tolerance"]
    first = 0
    while first < len(steps) and np.sum(steps[:first + 1]) <= span:
        first += 1
    endpoint_parts.append((0, min(first, len(steps))))
    last = len(steps)
    total_back = 0.0
    while last > 0 and total_back <= span:
        last -= 1
        total_back += steps[last]
    endpoint_parts.append((max(last, 0), len(steps)))
    long_off = [part for part in off_parts
                if part[2] > tolerance and not any(part[0] >= a and part[1] <= b for a, b in endpoint_parts)]
    longest_off = max(long_off, key=lambda part: part[2]) if long_off else (0, 0, 0.0)
    coverage = crack_coverage(context, points)
    reasons = []
    for item in coverage:
        if item["can_anchor"] and item["covered_fraction"] < MIN_CRACK_COVERAGE:
            reasons.append(f"does not follow confirmed short crack {item['index']}")
    if follows:
        reasons.append("runs along a marked painted line")
    if bridges:
        reasons.append("bridges an unphotographed gap")
    if long_off:
        frame_limit = round(tolerance * factor, 3)
        reasons.append(f"leaves visible linework for more than {frame_limit} {unit}")
    protected = 0.0
    uncovered_edge = []
    edge_total = 0.0
    if context.edge_distance is not None and len(context.edge_points):
        tree = cKDTree(points)
        distances = tree.query(context.edge_points)[0]
        steps_edge = np.linalg.norm(np.diff(context.edge_points, axis=0), axis=1)
        lengths = np.r_[steps_edge, 0.0]
        near = distances <= context.options["edge_contact"]
        protected = float(np.sum(lengths[near]))
        edge_total = float(np.sum(lengths))
        parts = stretch_flags(~near, lengths)
        for part in parts:
            if part[2] < 1.5:
                continue
            point = context.edge_points[(part[0] + part[1] - 1) // 2]
            uncovered_edge.append({
                "length": round(part[2] * factor, 3),
                "position": [round(float(point[0]) / frame.extent[0], 5),
                             round(float(point[1]) / frame.extent[1], 5)],
            })
        uncovered_edge.sort(key=lambda item: -item["length"])
        uncovered_edge = uncovered_edge[:MAX_AUDIT_PARTS]
    if edge_total:
        edge_fraction = protected / edge_total
    else:
        edge_fraction = None
    metrics = {
        "path_length": round(total * factor, 4),
        "tape_length": round(total * factor, 4),
        "length_unit": unit,
        "dual_capture_length": round(dual_length * factor, 4),
        "single_capture_length": round(single_length * factor, 4),
        "off_linework_length": round(off_length * factor, 4),
        "dual_capture_fraction": round(dual_length / total, 5),
        "evidence_fraction": round((dual_length + single_length) / total, 5),
        "longest_off_linework": round(longest_off[2] * factor, 4),
        "painted_crossings": len(crossings),
        "painted_crossing_length": round(sum(item[2] for item in crossings) * factor, 4),
        "painted_follow_length": round(sum(item[2] for item in follows) * factor, 4),
        "unsupported_bridge_length": round(sum(item[2] for item in bridges) * factor, 4),
        "edge_protected_length": round(protected * factor, 4),
        "edge_total_length": round(edge_total * factor, 4),
        "edge_protected_fraction": round(edge_fraction, 5) if edge_fraction is not None else None,
        "edge_uncovered": uncovered_edge,
        "confirmed_cracks": coverage,
        "valid": not reasons,
        "reasons": reasons,
    }
    return metrics, points


def plan_variant(context, grid, style, key):
    cost = candidate_cost(context, grid, style)
    graph = grid.matrix(cost)
    start, finish, gates = gate_nodes(grid, context)
    nodes = order_gates(graph, start, finish, gates)
    if nodes is None:
        raise InputError("The band ends and confirmed short cracks cannot be joined along visible linework.")
    metrics, points = path_metrics(context, grid, nodes)
    values = simplify_points(points, 0.9)
    normalized = [
        [round(float(x) / context.frame.extent[0], 6), round(float(y) / context.frame.extent[1], 6)]
        for x, y in values
    ]
    return {"key": key, "path": normalized, "metrics": metrics}


def tradeoff_text(first, second, preference):
    unit = first["metrics"]["length_unit"]
    a = first["metrics"]
    b = second["metrics"]
    dual_a = a["dual_capture_fraction"] * 100
    dual_b = b["dual_capture_fraction"] * 100
    edge_a = a["edge_protected_length"]
    edge_b = b["edge_protected_length"]
    sentence = (
        f"Route A follows two-capture evidence over {dual_a:.0f}% of its length and protects "
        f"{edge_a:.1f} {unit} of fragile edge; route B follows two-capture evidence over "
        f"{dual_b:.0f}% and protects {edge_b:.1f} {unit}."
    )
    weights = PRIORITY_WEIGHTS[preference]
    if a["edge_protected_fraction"] is None and b["edge_protected_fraction"] is None:
        guidance = "No fragile edge was marked, so the two-capture weight decides the choice."
    elif weights[0] == weights[1]:
        guidance = "A balanced preference weighs two-capture support and protected fragile edge equally."
    elif weights[0] > weights[1]:
        guidance = "An evidence preference keeps more of the route in two captures."
    else:
        guidance = "An edge preference keeps the strip on the fragile edge."
    return f"{sentence} {guidance}"


def recommendation(candidates, preference):
    if len(candidates) == 1:
        return candidates[0]["key"], "Only one admissible route connects the band ends through the confirmed short cracks."
    first, second = candidates
    weights = PRIORITY_WEIGHTS[preference]
    dual = [first["metrics"]["dual_capture_fraction"], second["metrics"]["dual_capture_fraction"]]
    edge = [first["metrics"]["edge_protected_fraction"] or 0.0, second["metrics"]["edge_protected_fraction"] or 0.0]
    span_dual = max(dual) - min(dual)
    span_edge = max(edge) - min(edge)
    score = []
    for index in range(2):
        share_dual = (dual[index] - min(dual)) / span_dual if span_dual > 1e-9 else 0.5
        share_edge = (edge[index] - min(edge)) / span_edge if span_edge > 1e-9 else 0.5
        score.append(weights[0] * share_dual + weights[1] * share_edge)
    chosen = 0 if score[0] >= score[1] else 1
    lead = candidates[chosen]
    other = candidates[1 - chosen]
    gap_dual = (lead["metrics"]["dual_capture_fraction"] - other["metrics"]["dual_capture_fraction"]) * 100
    gap_edge = lead["metrics"]["edge_protected_length"] - other["metrics"]["edge_protected_length"]
    if abs(score[0] - score[1]) < 1e-9:
        why = (
            f"the two routes trade off evenly, so route {lead['key']} is kept; "
            "favouring photographed evidence is the safer default, and the other route remains adoptable"
        )
    elif gap_dual >= 0 and gap_edge >= 0:
        why = f"route {lead['key']} is at least as good on both counts"
    elif gap_dual <= 0 and gap_edge <= 0:
        why = f"route {lead['key']} is weaker on both counts, so the {preference} preference is decisive"
    elif gap_dual > 0:
        why = f"route {lead['key']} keeps more of the strip on two-capture evidence"
    else:
        why = f"route {lead['key']} protects more fragile edge"
    text = tradeoff_text(first, second, preference)
    return lead["key"], f"{text} With a {preference} preference, {why}."


def plan_candidates(context):
    grid = Grid(context)
    preference_pairs = [("evidence", "A")]
    if context.edge_points is not None and len(context.edge_points):
        preference_pairs.append(("edge", "B"))
    candidates = []
    failures = []
    for style, key in preference_pairs:
        try:
            candidate = plan_variant(context, grid, CANDIDATE_STYLES[style], key)
        except InputError as error:
            failures.append({"key": key, "reason": str(error)})
            continue
        if not candidate["metrics"]["valid"]:
            failures.append({"key": key, "reason": "; ".join(candidate["metrics"]["reasons"])})
            continue
        candidates.append(candidate)
    if not candidates:
        detail = "; ".join(f"{item['key']}: {item['reason']}" for item in failures) or "no admissible path"
        raise InputError(f"No tape route satisfies the visible-crack, painted-line and support rules ({detail}).")
    if len(candidates) == 2:
        first = candidates[0]
        second = candidates[1]
        edge_first = first["metrics"]["edge_protected_fraction"] or 0.0
        edge_second = second["metrics"]["edge_protected_fraction"] or 0.0
        same = (
            abs(first["metrics"]["path_length"] - second["metrics"]["path_length"]) < 0.5
            and abs(first["metrics"]["dual_capture_fraction"] - second["metrics"]["dual_capture_fraction"]) < 0.005
            and abs(edge_first - edge_second) < 0.01
        )
        if same:
            candidates = candidates[:1]
    return candidates, failures


def band_rectangle(window, width_px):
    direction = window.finish - window.start
    span = np.linalg.norm(direction)
    side = np.array([-direction[1], direction[0]]) / span * (width_px / 2)
    return [window.start + side, window.finish + side, window.finish - side, window.start - side]


def closed_polygon(points):
    values = [(float(x), float(y)) for x, y in points]
    return [*values, values[0]]


def paint_route_scene(frame, record, *, band=None, traces=None, offset=(0, 0),
                      candidate_keys=None, candidate_colors=None, detail_color=None):
    image = base_rgba(frame)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = image.size
    if band is not None:
        rectangle = band_rectangle(band, record["options"]["band_width"])
        draw.line(closed_polygon(rectangle), fill=(*BAND_COLOR, 200), width=2)
    for points in record["painted_lines"]:
        values = np.asarray(points) * [width, height]
        draw.line([(float(x), float(y)) for x, y in values], fill=(*PAINT_COLOR, 255), width=2)
    for points in record["confirmed_cracks"]:
        values = np.asarray(points) * [width, height]
        draw.line([(float(x), float(y)) for x, y in values], fill=(*DUAL_COLOR, 255), width=3)
    for points in record["fragile_edges"]:
        values = np.asarray(points) * [width, height]
        draw.line([(float(x), float(y)) for x, y in values], fill=(*EDGE_COLOR, 255), width=3)
    if traces is not None and detail_color is not None:
        for thin in traces.values():
            rows, cols = np.nonzero(thin)
            tint = np.zeros((height, width, 4), dtype=np.uint8)
            tint[rows + offset[1], cols + offset[0]] = (*detail_color, 150)
            overlay.alpha_composite(Image.fromarray(tint))
    tape = max(2, int(round(record["options"]["tape_width"])))
    for candidate in record["candidates"]:
        if candidate_keys is not None and candidate["key"] not in candidate_keys:
            continue
        color = (candidate_colors or CANDIDATE_COLORS)[candidate["key"]]
        values = np.asarray(candidate["path"]) * [width, height]
        coordinates = [(float(x), float(y)) for x, y in values]
        draw.line(coordinates, fill=(*color, 90), width=tape, joint="curve")
        draw.line(coordinates, fill=(*color, 255), width=2, joint="curve")
    return Image.alpha_composite(image, overlay)


def render_route_overlay(context, record, candidate_key=None, detail=False):
    keys = {candidate_key} if candidate_key is not None else None
    colors = {candidate_key: ADOPTED_COLOR} if candidate_key is not None else None
    return paint_route_scene(
        context.frame, record, band=context.band,
        traces=context.evidence.traces if detail else None, offset=(context.band.left, context.band.top),
        candidate_keys=keys, candidate_colors=colors, detail_color=DUAL_COLOR,
    )


def plan_route(store, tile_id, payload):
    tile = store.tile(tile_id)
    record = read_route_payload(payload)
    context = build_context(store, tile, record)
    candidates, failures = plan_candidates(context)
    key, text = recommendation(candidates, record["preference"])
    plan_record = {**record, "candidates": candidates, "options": record["options"]}
    saved = store.save_route({
        "tile_id": tile["id"],
        "tile_version": tile["version"],
        "status": "planned",
        **record,
        "candidates": candidates,
        "recommendation": key,
        "tradeoff": text,
        "failures": failures,
        "plane": {
            "size": list(context.frame.extent),
            "corners": tile["plane"]["corners"],
            "base_photo_id": tile["plane"]["photo_id"],
            "scale": list(context.frame.scale) if context.frame.scale else None,
        },
        "evidence": {
            "photos": [
                {"id": photo_id, "role": context.evidence.roles.get(photo_id),
                 "traced_pixels": int(np.count_nonzero(thin))}
                for photo_id, thin in context.evidence.traces.items()
            ],
            "skipped": context.evidence.skipped,
            "thresholds": context.evidence.thresholds,
            "dual_capture_pixels": int(np.count_nonzero(context.evidence.count >= 2)),
            "single_capture_pixels": int(np.count_nonzero(context.evidence.count == 1)),
        },
        "cracks": context.cracks,
        "picture": stored_preview(render_route_overlay(context, plan_record, detail=True), store.blobs),
    })
    return saved


def route_summary(record):
    adopted = record.get("adopted") or {}
    chosen = next((item for item in record["candidates"] if item["key"] == adopted.get("key")), None)
    return {
        "id": record["id"],
        "tile_id": record["tile_id"],
        "status": record["status"],
        "created_at": record["created_at"],
        "adopted_at": adopted.get("adopted_at"),
        "adopted_key": adopted.get("key"),
        "recommendation": record.get("recommendation"),
        "tradeoff": record.get("tradeoff"),
        "preference": record["preference"],
        "length_unit": record["candidates"][0]["metrics"]["length_unit"] if record["candidates"] else "px",
        "path_length": chosen["metrics"]["path_length"] if chosen else
        (record["candidates"][0]["metrics"]["path_length"] if record["candidates"] else None),
        "picture_url": f"/api/routes/{record['id']}/image/plan",
    }


def adopt_route(store, route_id, payload):
    data = dictionary(payload, "Route adoption")
    key = enumeration(data.get("candidate"), CANDIDATE_NAMES, "Route candidate")
    record = store.route(route_id)
    if record["status"] == "adopted":
        if record["adopted"]["key"] == key:
            return record
        raise ConflictError("A tape route is already adopted for this specimen. Add captures or edges without changing it.")
    if store.adopted_route(record["tile_id"]):
        raise ConflictError("This specimen already has an adopted tape route. It is frozen against later captures and edge marks.")
    chosen = next((item for item in record["candidates"] if item["key"] == key), None)
    if chosen is None:
        raise InputError("That candidate is not part of this saved plan.")
    if not chosen["metrics"]["valid"]:
        raise InputError("Only an admissible candidate can be adopted.")
    picture = render_overlay_for(store, record, key)
    frozen = {
        "key": key,
        "path": chosen["path"],
        "metrics": chosen["metrics"],
        "tape_width": record["options"]["tape_width"],
        "band_width": record["options"]["band_width"],
        "unit": chosen["metrics"]["length_unit"],
        "plane": record["plane"],
        "source_tile_version": record["tile_version"],
        "picture": stored_preview(picture, store.blobs),
    }
    return store.adopt_route(route_id, key, frozen, record["tradeoff"], record["recommendation"])


def render_overlay_for(store, record, key):
    tile = store.tile(record["tile_id"])
    frame = plane_frame(store, tile)
    return paint_route_scene(frame, record, candidate_keys={key}, candidate_colors={key: ADOPTED_COLOR})


def current_adopted(store, tile_id):
    tile = store.tile(tile_id)
    record = store.adopted_route(tile["id"])
    if record is None:
        raise MissingError("No tape route has been adopted for this specimen.")
    return record


def audit_observation(record, observation, scale):
    outline = observation.get("outline") or []
    width, height = record["adopted"]["plane"]["size"]
    tape_radius = record["adopted"]["tape_width"] / 2
    if len(outline) < 3:
        return {
            "key": observation["key"],
            "state": "no_outline",
            "covered_fraction": None,
            "covered_area_px": 0,
            "covered_area_mm2": None,
            "uncovered_area_px": None,
            "uncovered_fraction": None,
            "uncovered_parts": [],
            "note": "This saved spot has no stored outline to test.",
        }
    points = np.asarray(outline, dtype=np.float64) * [width, height]
    minimum = points.min(axis=0) - tape_radius - 3
    maximum = points.max(axis=0) + tape_radius + 3
    left = max(0, int(np.floor(minimum[0])))
    top = max(0, int(np.floor(minimum[1])))
    right = min(width, int(np.ceil(maximum[0])) + 1)
    bottom = min(height, int(np.ceil(maximum[1])) + 1)
    if right - left < 2 or bottom - top < 2:
        return {
            "key": observation["key"], "state": "no_outline", "covered_fraction": None,
            "covered_area_px": 0, "covered_area_mm2": None, "uncovered_area_px": None,
            "uncovered_fraction": None, "uncovered_parts": [],
            "note": "This saved spot lies outside the specimen plane.",
        }
    scaled = (points - [left, top]) * scale
    grid = Image.new("1", ((right - left) * scale, (bottom - top) * scale), 0)
    ImageDraw.Draw(grid).polygon([(float(x), float(y)) for x, y in scaled], fill=1)
    polygon = np.asarray(grid, dtype=bool)
    if not polygon.any():
        # A very small saved outline can fall between scaled pixels. Keep at least its centroid.
        centre = np.rint(points.mean(axis=0) - [left, top]).astype(int)
        row = int(np.clip(centre[1] * scale, 0, grid.size[1] - 1))
        col = int(np.clip(centre[0] * scale, 0, grid.size[0] - 1))
        polygon[row, col] = True
        grid.putpixel((col, row), 1)
    route = np.asarray(record["adopted"]["path"], dtype=np.float64) * [width, height]
    route = (route - [left, top]) * scale
    route_mask = Image.new("1", grid.size, 0)
    artist = ImageDraw.Draw(route_mask)
    artist.line([(float(x), float(y)) for x, y in route], fill=1, width=max(1, int(round(scale))))
    distances = ndimage.distance_transform_edt(~np.asarray(route_mask, dtype=bool))
    covered = polygon & (distances <= tape_radius * scale)
    uncovered = polygon & ~covered
    factor = scale * scale
    polygon_area = float(np.count_nonzero(polygon)) / factor
    covered_area = float(np.count_nonzero(covered)) / factor
    uncovered_area = float(np.count_nonzero(uncovered)) / factor
    fraction = covered_area / polygon_area if polygon_area else 0.0
    physical = record["adopted"]["plane"].get("scale")
    mm2 = (float(physical[0] * physical[1]) * covered_area) if physical else None
    labels, count = ndimage.label(uncovered, structure=ndimage.generate_binary_structure(2, 2))
    parts = []
    for index in range(1, count + 1):
        cells = int(np.count_nonzero(labels == index))
        if cells < max(1, scale * scale / 4):
            continue
        rows, cols = np.nonzero(labels == index)
        centre = np.array([cols.mean(), rows.mean()]) / scale + [left, top]
        bounds = (cols.min() / scale + left, rows.min() / scale + top,
                  (cols.max() + 1) / scale + left, (rows.max() + 1) / scale + top)
        edge = labels == index
        edge &= ~ndimage.binary_erosion(edge, structure=ndimage.generate_binary_structure(2, 2))
        boundary_rows, boundary_cols = np.nonzero(edge)
        angles = np.arctan2(boundary_rows - rows.mean(), boundary_cols - cols.mean())
        order = np.argsort(angles, kind="stable")
        stride = max(1, int(math.ceil(len(order) / 24)))
        outline_points = [
            [round(float((boundary_cols[i] / scale + left) / width), 5),
             round(float((boundary_rows[i] / scale + top) / height), 5)]
            for i in order[::stride]
        ]
        parts.append({
            "area_px": round(float(cells) / factor, 3),
            "area_mm2": round(float(physical[0] * physical[1]) * cells / factor, 4) if physical else None,
            "center": [round(float(centre[0]) / width, 5), round(float(centre[1]) / height, 5)],
            "bounds": [round(float(bounds[0]) / width, 5), round(float(bounds[1]) / height, 5),
                       round(float(bounds[2]) / width, 5), round(float(bounds[3]) / height, 5)],
            "outline": outline_points,
        })
    parts.sort(key=lambda item: -item["area_px"])
    parts = parts[:MAX_AUDIT_PARTS]
    if fraction >= 0.95:
        state = "covered"
    elif fraction > 0.02:
        state = "partial"
    else:
        state = "uncovered"
    if fraction >= 0.95:
        note = "The whole stored outline lies inside the adopted tape strip."
    elif fraction > 0.02:
        note = "Part of the stored outline lies outside the adopted tape strip."
    else:
        note = "The stored outline lies outside the adopted tape strip."
    return {
        "key": observation["key"],
        "type": observation.get("type"),
        "center": observation.get("normalized_center"),
        "outline_area_px": round(polygon_area, 3),
        "outline_area_mm2": round(float(physical[0] * physical[1]) * polygon_area, 4) if physical else None,
        "covered_area_px": round(covered_area, 3),
        "covered_area_mm2": round(mm2, 4) if mm2 is not None else None,
        "uncovered_area_px": round(uncovered_area, 3),
        "uncovered_fraction": round(1 - fraction, 5),
        "covered_fraction": round(fraction, 5),
        "state": state,
        "uncovered_parts": parts,
        "note": note,
    }


def audit_result(store, route_record, result_id, only_key=None):
    result = store.result(result_id)
    tile = store.tile(route_record["tile_id"])
    if result["tile_id"] != tile["id"]:
        raise InputError("That Surface observations measurement belongs to another specimen.")
    if result["kind"] != "defects":
        raise InputError("Only Surface observations measurements can be tested against a tape route.")
    observations = result["metrics"].get("observations", [])
    if only_key:
        observations = [item for item in observations if item["key"] == only_key]
        if not observations:
            raise MissingError("That saved spot is no longer in this Surface observations measurement.")
    scale = AUDIT_SCALE
    items = [audit_observation(route_record, item, scale) for item in observations]
    if only_key and items:
        items[0]["picture_url"] = (
            f"/api/routes/{route_record['id']}/audit/{result_id}/{only_key}.png"
        )
    covered = sum(1 for item in items if item["state"] == "covered")
    partial = sum(1 for item in items if item["state"] == "partial")
    uncovered = sum(1 for item in items if item["state"] == "uncovered")
    missing = sum(1 for item in items if item["state"] == "no_outline")
    outlined = [item for item in items if item["state"] != "no_outline"]
    outline_total = sum(item["outline_area_px"] or 0 for item in outlined)
    covered_total = sum(item["covered_area_px"] or 0 for item in outlined)
    return {
        "route_id": route_record["id"],
        "route_key": route_record["adopted"]["key"],
        "result_id": result["id"],
        "result_version": result["version"],
        "specimen_version": result["source_version"],
        "items": items,
        "summary": {
            "observed": len(items),
            "covered": covered,
            "partial": partial,
            "uncovered": uncovered,
            "without_outline": missing,
            "covered_fraction": round(covered_total / outline_total, 5) if outline_total > 0 else None,
        },
        "note": "Coverage is tested inside each saved outline against the frozen tape strip, not against the live photograph. Spots without a stored outline are counted separately.",
    }


def audit_picture(store, route_record, result_id, key):
    result = store.result(result_id)
    observation = next((item for item in result["metrics"].get("observations", []) if item["key"] == key), None)
    if observation is None:
        raise MissingError("That saved spot is no longer in this Surface observations measurement.")
    tile = store.tile(route_record["tile_id"])
    frame = plane_frame(store, tile)
    image = base_rgba(frame)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = image.size
    route = np.asarray(route_record["adopted"]["path"]) * [width, height]
    coordinates = [(float(x), float(y)) for x, y in route]
    tape = max(2, int(round(route_record["adopted"]["tape_width"])))
    draw.line(coordinates, fill=(*ADOPTED_COLOR, 90), width=tape, joint="curve")
    draw.line(coordinates, fill=(*ADOPTED_COLOR, 255), width=2, joint="curve")
    item = audit_observation(route_record, observation, AUDIT_SCALE)
    outline = observation.get("outline") or []
    if len(outline) >= 3:
        points = np.asarray(outline) * [width, height]
        draw.polygon([(float(x), float(y)) for x, y in points], fill=(*OUTLINE_COLOR, 40))
        draw.line([*[(float(x), float(y)) for x, y in points], (float(points[0][0]), float(points[0][1]))],
                  fill=(*OUTLINE_COLOR, 230), width=2)
    for part in item.get("uncovered_parts", []):
        if len(part["outline"]) >= 3:
            points = np.asarray(part["outline"]) * [width, height]
            draw.polygon([(float(x), float(y)) for x, y in points], fill=(*UNCOVERED_COLOR, 160))
            draw.line([*[(float(x), float(y)) for x, y in points],
                       (float(points[0][0]), float(points[0][1]))], fill=(*UNCOVERED_COLOR, 255), width=2)
    if len(outline) >= 3:
        covered_outline = np.asarray(outline) * [width, height]
        draw.text((float(covered_outline[0][0]) + 4, float(covered_outline[0][1]) - 12),
                  f"{item['covered_fraction'] * 100:.0f}% covered" if item.get("covered_fraction") is not None else "no outline",
                  fill=(*OUTLINE_COLOR, 255), stroke_width=2, stroke_fill=SHADOW)
    return Image.alpha_composite(image, overlay)
