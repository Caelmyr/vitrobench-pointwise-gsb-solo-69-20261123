"""Cutting plans for replacement panes cut from standard stock sheets.

A plan is derived from a saved Glass pane census. Only panes whose whole edge
is confirmed in the registered photograph ("bounded" cells) can be cut; cells
that touch a support boundary, and therefore may continue out of frame, are
held back. Every recorded fact is snapshotted into the saved plan so that
later photographs or threshold changes never move an adopted layout or the
original-image positions it refers to.

Conventions
-----------
* Plane millimetres use the registered photograph axes, x right and y down,
  with the origin at the upper-left survey corner.
* Each pane carries a grain angle (degrees clockwise from the plane x axis).
  Its cutting rectangle is the smallest axis-aligned box that contains the
  pane after rotating it by minus that angle, so the rectangle's long axis is
  parallel to the marked grain.
* A pane may be turned 180 degrees on the sheet (its rectangle swaps width
  and height); it is never mirrored, because flipping rolled glass reverses
  the face and is not an allowed workshop operation.
"""
from dataclasses import dataclass
import math

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .art import INK, SHADOW, font, stored_preview
from .geometry import region_domain, specimen_frame
from .glass import sampled_panel
from .model import (
    ConflictError,
    InputError,
    MissingError,
    dictionary,
    identity,
    integer,
    numeric_pair,
    scalar,
    string,
)
from .photos import luminance

STRATEGIES = ("economy", "neighbor")
MAX_PANES = 300
MAX_SHEETS = 60
CONTOUR_STEP_MM = 0.25
DIAGRAM_WIDTH = 760
PALETTE = ((144, 232, 174), (246, 139, 96), (126, 187, 218), (228, 206, 124), (178, 150, 214))


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #

def cutting_spec(payload):
    data = dictionary(payload, "Cutting plan")
    spec = {
        "census_result_id": identity(data.get("census_result_id"), "Census result ID"),
        "title": string(data.get("title"), "Plan title", maximum=115),
        "strategy": data.get("strategy", "economy"),
        "sheet_width_mm": scalar(data.get("sheet_width_mm"), "Stock sheet width", lower=10, upper=10000),
        "sheet_height_mm": scalar(data.get("sheet_height_mm"), "Stock sheet height", lower=10, upper=10000),
        "kerf_mm": scalar(data.get("kerf_mm"), "Saw kerf / cut gap", lower=0, upper=200),
        "margin_mm": scalar(data.get("margin_mm"), "Edge margin", lower=0, upper=1000),
        "grain_angles": _grain_angles(data.get("grain_angles")),
    }
    if spec["strategy"] not in STRATEGIES:
        raise InputError("Strategy must be 'economy' or 'neighbor'.")
    if spec["margin_mm"] * 2 >= min(spec["sheet_width_mm"], spec["sheet_height_mm"]):
        raise InputError("The edge margin leaves no usable field on the stock sheet.")
    return spec


def _grain_angles(raw):
    if raw is None:
        return {}
    table = dictionary(raw, "Grain directions")
    angles = {}
    for key, value in table.items():
        pane = integer(int(key), "Pane number", lower=1, upper=MAX_PANES)
        angles[pane] = scalar(value, f"Grain direction for pane {pane}", lower=0, upper=179.999)
    return angles


# --------------------------------------------------------------------------- #
# Pane extraction: reproduce the census masks and keep only fully bounded cells
# --------------------------------------------------------------------------- #

def census_frame(store, result):
    if result.get("kind") != "panes":
        raise InputError("Choose a saved Glass pane census as the cutting-plan source.")
    tile = store.tile(result["tile_id"])
    params = result["parameters"]
    frame = specimen_frame(store.blobs, tile, params["photo_id"])
    if frame.scale is None:
        raise InputError("Enter the surveyed specimen width and height before laying out cuts in millimetres.")
    region = None
    if params.get("region_id"):
        from .geometry import active_region
        region = active_region(tile, params["region_id"])
    area = region_domain(frame.extent, tile, region)
    return tile, frame, area, params["options"]


def pane_components(frame, area, options):
    """Recompute the exact connected cells the census used, with support state."""
    support = sampled_panel(frame, area)
    light = luminance(frame.linear)
    metal = support & (light <= options["cutoff"])
    sealed = ndimage.binary_closing(metal, iterations=1) & support
    labels, count = ndimage.label(support & ~(metal | sealed))
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    boundary = ndimage.binary_dilation(~support) & support
    rects = ndimage.find_objects(labels)
    chosen = sorted(
        (label for label in range(1, count + 1) if sizes[label] >= options["minimum_area"]),
        key=lambda label: (sizes[label], -label),
        reverse=True,
    )
    cells = {}
    for pane, label in enumerate(chosen[:MAX_PANES], 1):
        rect = rects[label - 1]
        component = labels[rect] == label
        cells[pane] = {
            "mask": component,
            "rect": rect,
            "support_edge": bool(np.any(boundary[rect] & component)),
        }
    return cells, frame.scale


def contour_points(mask, rect, scale):
    """Outermost pixel ring of a cell as a millimetre polygon in plane axes."""
    ring = mask & ~ndimage.binary_erosion(mask)
    yy, xx = np.nonzero(ring)
    left, top = rect[1].start, rect[0].start
    points = np.column_stack(((xx + left + 0.5) * scale[0], (yy + top + 0.5) * scale[1]))
    centre = points.mean(axis=0)
    order = np.argsort(np.arctan2(points[:, 1] - centre[1], points[:, 0] - centre[0]))
    ordered = points[order]
    keep = [0]
    for index in range(1, len(ordered)):
        if np.linalg.norm(ordered[index] - ordered[keep[-1]]) >= CONTOUR_STEP_MM:
            keep.append(index)
    if len(keep) > 2 and np.linalg.norm(ordered[keep[0]] - ordered[keep[-1]]) < CONTOUR_STEP_MM:
        keep.pop()
    return ordered[keep].round(3).tolist()


# --------------------------------------------------------------------------- #
# Grain-aligned cutting rectangles
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Piece:
    pane: int
    angle: float
    width: float          # grain-aligned rectangle size along the rotated x axis
    height: float
    anchor: tuple         # plane-mm point that maps to the rotated-box origin
    rotate: tuple         # cos/sin of the grain angle
    polygon: list         # plane-mm outline of the confirmed pane edge


def _rotate_points(points, cos_t, sin_t):
    return np.asarray(points) @ np.array([[cos_t, sin_t], [-sin_t, cos_t]])


def build_pieces(cells, scale, grain_angles, census_rows):
    row_state = {row["pane"]: row["state"] for row in census_rows}
    pieces, excluded = [], []
    for pane, cell in sorted(cells.items()):
        polygon = contour_points(cell["mask"], cell["rect"], scale)
        if cell["support_edge"] or row_state.get(pane) != "bounded":
            excluded.append({
                "pane": pane,
                "reason": "touches the edge of photographic support; its full outline is not confirmed",
                "support_edge": True,
            })
            continue
        angle = float(grain_angles.get(pane, 0.0))
        theta = math.radians(angle)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        rotated = _rotate_points(polygon, cos_t, sin_t)
        low, high = rotated.min(axis=0), rotated.max(axis=0)
        width, height = float(high[0] - low[0]), float(high[1] - low[1])
        if min(width, height) <= 0:
            raise InputError(f"Pane {pane} collapsed to a line; it cannot be cut.")
        # Rotated-box lower-left in the rotated (grain) coordinate frame. The
        # plane origin is the survey corner, so grain-local coords of a plane
        # point are its rotated coordinates shifted by this minimum.
        anchor = (float(low[0]), float(low[1]))
        pieces.append(Piece(
            pane=pane, angle=angle, width=width, height=height,
            anchor=anchor,
            rotate=(cos_t, sin_t), polygon=polygon,
        ))
    return pieces, excluded


def oriented_dims(piece, flipped):
    """Sheet-space rectangle dimensions for an orientation."""
    return (piece.height, piece.width) if flipped else (piece.width, piece.height)


# --------------------------------------------------------------------------- #
# Shelf bin packing across whole stock sheets
# --------------------------------------------------------------------------- #

@dataclass
class Shelf:
    top: float
    height: float
    used: float = 0.0


def _shelf_pack(units, field_w, field_h, kerf):
    """First-fit decreasing-height shelf packing with sheet rollover.

    Units are dicts {key, width, height, meta}. Returns one layout list per
    sheet, or None when a unit cannot be placed within MAX_SHEETS sheets.
    """
    sheets = []          # list of sheets; each sheet is a list of placements
    shelves = []         # shelves of the sheet currently being filled
    top = 0.0

    def open_sheet():
        nonlocal shelves, top
        shelves = []
        top = 0.0
        sheets.append([])

    open_sheet()
    for unit in units:
        width, height = unit["width"], unit["height"]
        target = None
        for shelf in shelves:
            if height <= shelf.height + 1e-9 and shelf.used + width <= field_w + 1e-9:
                target = shelf
                break
        if target is not None:
            x = target.used
            target.used += width + kerf
            y = target.top
        else:
            if top + height > field_h + 1e-9:
                if len(sheets) >= MAX_SHEETS:
                    return None
                open_sheet()
            x, y = 0.0, top
            shelves.append(Shelf(top, height, width + kerf))
            top += height + kerf
        sheets[-1].append({**unit["meta"], "x": x, "y": y,
                           "width": width, "height": height,
                           "sheet": len(sheets)})
    return sheets


def _economize(pieces, field_w, field_h, kerf, allow_flip, order):
    units = []
    for piece in pieces:
        normal_w, normal_h = oriented_dims(piece, False)
        normal_fits = normal_w <= field_w + 1e-9 and normal_h <= field_h + 1e-9
        flip_w, flip_h = oriented_dims(piece, True)
        # After a 180 degree turn the footprint width/height swap.
        flip_fits = allow_flip and flip_w <= field_w + 1e-9 and flip_h <= field_h + 1e-9
        if not (normal_fits or flip_fits):
            return None
        # Prefer the turned orientation when it fits, since its smaller
        # footprint height packs more panes onto a shelf.
        if flip_fits:
            width, height, flipped = flip_w, flip_h, True
        else:
            width, height, flipped = normal_w, normal_h, False
        units.append({"key": piece.pane, "width": width, "height": height,
                      "meta": {"pane": piece.pane, "flipped": flipped,
                               "grain_width": piece.width, "grain_height": piece.height}})
    if order == "area":
        units.sort(key=lambda unit: -(unit["width"] * unit["height"]))
    else:
        units.sort(key=lambda unit: -unit["height"])
    return _shelf_pack(units, field_w, field_h, kerf)


def pack_economy(pieces, field_w, field_h, kerf):
    candidates = []
    for allow_flip in (False, True):
        for order in ("height", "area"):
            plan = _economize(pieces, field_w, field_h, kerf, allow_flip, order)
            if plan:
                candidates.append(plan)
    if not candidates:
        return None
    return min(candidates, key=lambda plan: (len(plan), _packed_height(plan)))


def adjacency_groups(pieces, cells):
    """Group panes whose photographed cells share (or nearly share) a came line."""
    panes = [piece.pane for piece in pieces]
    row_start = min(cells[pane]["rect"][0].start for pane in panes)
    row_stop = max(cells[pane]["rect"][0].stop for pane in panes)
    col_start = min(cells[pane]["rect"][1].start for pane in panes)
    col_stop = max(cells[pane]["rect"][1].stop for pane in panes)
    full = (slice(row_start, row_stop), slice(col_start, col_stop))
    masks = {}
    for pane in panes:
        cell = cells[pane]
        rect = cell["rect"]
        view = np.zeros((row_stop - row_start, col_stop - col_start), dtype=bool)
        local = (slice(rect[0].start - row_start, rect[0].stop - row_start),
                 slice(rect[1].start - col_start, rect[1].stop - col_start))
        view[local] = cell["mask"]
        masks[pane] = view
    neighbors = {pane: set() for pane in panes}
    # The came between two real neighbours is several pixels wide in the
    # registered plane; dilate across roughly one came width to detect the
    # shared joint without merging panes that only meet at far corners.
    reach = 8
    for index, pane_a in enumerate(panes):
        expanded = ndimage.binary_dilation(masks[pane_a], iterations=reach)
        for pane_b in panes[index + 1:]:
            if np.any(expanded & masks[pane_b]):
                neighbors[pane_a].add(pane_b)
                neighbors[pane_b].add(pane_a)
    groups, seen = [], set()
    for pane in panes:
        if pane in seen:
            continue
        stack, group = [pane], []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            group.append(current)
            stack.extend(sorted(neighbors[current] - seen))
        groups.append(sorted(group))
    return groups


def pack_neighbor(pieces, cells, field_w, field_h, kerf):
    """Pack each original-window adjacency group so it stays on one sheet."""
    groups = adjacency_groups(pieces, cells)
    by_pane = {piece.pane: piece for piece in pieces}
    block_sheets = []
    for group in groups:
        units = []
        for pane in group:
            piece = by_pane[pane]
            width, height = oriented_dims(piece, False)  # neighbours keep orientation
            if width > field_w + 1e-9 or height > field_h + 1e-9:
                raise InputError(
                    f"Pane {pane} is larger than the usable stock field even without neighbours; "
                    "increase the sheet size or lower the margin."
                )
            units.append({"key": pane, "width": width, "height": height,
                          "meta": {"pane": pane, "flipped": False,
                                   "grain_width": piece.width, "grain_height": piece.height}})
        units.sort(key=lambda unit: -unit["height"])
        packed = _shelf_pack(units, field_w, field_h, kerf)
        if packed is None or len(packed) > 1:
            raise InputError(
                "Original neighbouring panes " + ", ".join(map(str, group)) +
                " cannot be kept on one stock sheet at the given sheet size, kerf and margin. "
                "Choose the economy strategy or use larger stock."
            )
        block_sheets.append(packed[0])

    # Place the intact group blocks onto stock sheets, largest first.
    blocks = [{
        "panes": group,
        "width": max(p["x"] + p["width"] for p in block),
        "height": max(p["y"] + p["height"] for p in block),
        "block": block,
    } for group, block in zip(groups, block_sheets)]
    blocks.sort(key=lambda block: -(block["width"] * block["height"]))

    sheets = []
    for block in blocks:
        placed = False
        for sheet in sheets:
            if _block_fits(sheet, block, field_w, field_h, kerf):
                _place_block(sheet, block, field_w, kerf)
                placed = True
                break
        if not placed:
            if len(sheets) >= MAX_SHEETS:
                return None, groups
            sheet = []
            _place_block(sheet, block, field_w, kerf)
            sheets.append(sheet)
    numbered = []
    for index, sheet in enumerate(sheets, 1):
        numbered.append([dict(placement, sheet=index) for placement in sheet])
    return numbered, groups


def _sheet_extent(sheet):
    if not sheet:
        return 0.0, 0.0
    return (
        max(p["x"] + p["width"] for p in sheet),
        max(p["y"] + p["height"] for p in sheet),
    )


def _block_fits(sheet, block, field_w, field_h, kerf):
    """Try right of the current content, then below it."""
    max_x, max_y = _sheet_extent(sheet)
    right = max_x + kerf if sheet else 0.0
    if right + block["width"] <= field_w + 1e-9 and block["height"] <= field_h + 1e-9:
        return True
    below = max_y + kerf if sheet else 0.0
    return block["width"] <= field_w + 1e-9 and below + block["height"] <= field_h + 1e-9


def _place_block(sheet, block, field_w, kerf):
    max_x, max_y = _sheet_extent(sheet)
    right = max_x + kerf if sheet else 0.0
    if right + block["width"] <= field_w + 1e-9:
        offset_x, offset_y = right, 0.0
    else:
        offset_x, offset_y = 0.0, (max_y + kerf if sheet else 0.0)
    for placement in block["block"]:
        sheet.append(dict(placement, x=placement["x"] + offset_x, y=placement["y"] + offset_y))


def _packed_height(sheets):
    return sum(
        max((p["y"] + p["height"] for p in sheet), default=0.0) for sheet in sheets
    )


def flatten_layout(sheets):
    layout = []
    for sheet in sheets:
        for placement in sheet:
            layout.append(placement)
    layout.sort(key=lambda item: (item["sheet"], item["pane"]))
    return layout


# --------------------------------------------------------------------------- #
# Coordinate transforms: plane mm -> grain frame -> stock sheet mm
# --------------------------------------------------------------------------- #

def grain_local(piece, plane_mm):
    """Grain-frame millimetres relative to the piece rectangle's lower-left."""
    cos_t, sin_t = piece["rotate"]
    rotated_x = cos_t * plane_mm[0] - sin_t * plane_mm[1]
    rotated_y = sin_t * plane_mm[0] + cos_t * plane_mm[1]
    return rotated_x - piece["anchor"][0], rotated_y - piece["anchor"][1]


def sheet_position(piece, placement, margin, plane_mm):
    """Stock-sheet millimetres for a plane-mm point, both axes top-down.

    The sheet origin is its upper-left corner; x grows right and y grows down,
    matching the cutting diagram. Normal placement maps the grain-frame box
    directly. A 180 degree turn swaps the footprint (grain_h × grain_w) and
    reverses both grain-frame axes: grain x runs toward sheet -y and grain y
    toward sheet -x. It is a half-turn in the plane, never a mirror flip.
    """
    local_x, local_y = grain_local(piece, plane_mm)  # 0..grain_w, 0..grain_h
    grain_w = placement["grain_width"]
    grain_h = placement["grain_height"]
    if placement["flipped"]:
        return (
            placement["x"] + margin + (grain_h - local_y),
            placement["y"] + margin + (grain_w - local_x),
        )
    return (
        placement["x"] + margin + local_x,
        placement["y"] + margin + local_y,
    )


def sheet_polygon(piece, placement, margin):
    points = []
    for px, py in piece["polygon"]:
        sx, sy = sheet_position(piece, placement, margin, (px, py))
        points.append((round(sx, 3), round(sy, 3)))
    return points


def _point_in_polygon(point, polygon):
    x, y = point
    inside = False
    count = len(polygon)
    for index in range(count):
        ax, ay = polygon[index]
        bx, by = polygon[(index + 1) % count]
        if (ay > y) != (by > y):
            crossing = ax + (y - ay) * (bx - ax) / (by - ay)
            if x < crossing:
                inside = not inside
    return inside


def locate_point(plan, pane_number, point):
    pane_number = integer(pane_number, "Pane number", lower=1, upper=MAX_PANES)
    u, v = numeric_pair(point, "Edge point", lower=0, upper=1)
    piece = next((item for item in plan["pieces"] if item["pane"] == pane_number), None)
    if piece is None:
        raise MissingError(f"Pane {pane_number} is not part of this cutting plan.")
    extent = plan["snapshot"]["frame"]["extent"]
    scale = plan["snapshot"]["frame"]["scale"]
    plane_mm = (u * extent[0] * scale[0], v * extent[1] * scale[1])
    if not _point_in_polygon(plane_mm, piece["polygon"]):
        raise InputError(
            f"The marked point is not inside the confirmed edge of pane {pane_number}. "
            "Pick a point on the pane interior or its visible edge."
        )
    placement = next(item for item in plan["layout"] if item["pane"] == pane_number)
    margin = plan["spec"]["margin_mm"]
    sheet_w = plan["spec"]["sheet_width_mm"]
    sheet_h = plan["spec"]["sheet_height_mm"]
    sx, sy = sheet_position(piece, placement, margin, plane_mm)
    # Sheet coordinates are top-down: x from the left edge, y from the top edge.
    return {
        "pane": pane_number,
        "point_normalized": [round(u, 6), round(v, 6)],
        "plane_mm": [round(plane_mm[0], 3), round(plane_mm[1], 3)],
        "sheet": placement["sheet"],
        "sheet_size_mm": [sheet_w, sheet_h],
        "distance_from_left_mm": round(sx, 3),
        "distance_from_top_mm": round(sy, 3),
        "distance_from_right_mm": round(sheet_w - sx, 3),
        "distance_from_bottom_mm": round(sheet_h - sy, 3),
        "turned_180": bool(placement["flipped"]),
        "grain_angle_deg": piece["angle"],
    }


# --------------------------------------------------------------------------- #
# Rendering (all images derive from the frozen plan snapshot)
# --------------------------------------------------------------------------- #

def render_sheet(plan, sheet_index):
    spec = plan["spec"]
    sheet_w, sheet_h = spec["sheet_width_mm"], spec["sheet_height_mm"]
    diagram_scale = min(DIAGRAM_WIDTH / sheet_w, 900 / sheet_h, 6.0)
    px_w = max(60, int(round(sheet_w * diagram_scale)))
    px_h = max(60, int(round(sheet_h * diagram_scale)))
    image = Image.new("RGBA", (px_w, px_h), (237, 240, 242, 255))
    draw = ImageDraw.Draw(image)
    margin = spec["margin_mm"]
    draw.rectangle(
        [margin * diagram_scale, margin * diagram_scale,
         (sheet_w - margin) * diagram_scale, (sheet_h - margin) * diagram_scale],
        outline=(120, 138, 146, 255), width=2,
    )
    typeface = font(max(12, int(px_w / 42)))
    small = font(max(10, int(px_w / 58)))
    pieces = {item["pane"]: item for item in plan["pieces"]}
    for placement in plan["layout"]:
        if placement["sheet"] != sheet_index:
            continue
        piece = pieces[placement["pane"]]
        color = PALETTE[placement["pane"] % len(PALETTE)] + (255,)
        polygon_px = [
            (x * diagram_scale, y * diagram_scale)
            for x, y in sheet_polygon(piece, placement, margin)
        ]
        draw.polygon(polygon_px, fill=color, outline=SHADOW)
        cx = (placement["x"] + margin + placement["width"] / 2) * diagram_scale
        cy = (placement["y"] + margin + placement["height"] / 2) * diagram_scale
        label = f"#{placement['pane']}" + (" ⟲180" if placement["flipped"] else "")
        draw.text((cx - 16, cy - 8), label, fill=INK, font=typeface,
                  stroke_width=2, stroke_fill=(255, 255, 255, 255))
        _grain_arrow(draw, placement, margin, diagram_scale, small)
    draw.text((8, 6), f"Stock sheet {sheet_index} of {plan['sheet_count']} · "
                      f"{sheet_w:g} × {sheet_h:g} mm", fill=SHADOW, font=small)
    draw.text((8, px_h - 18),
              f"margin {margin:g} mm · kerf {spec['kerf_mm']:g} mm · {plan['strategy']} layout",
              fill=SHADOW, font=small)
    return image


def _grain_arrow(draw, placement, margin, diagram_scale, typeface):
    # Grain runs along the footprint x axis normally, and along -y after a
    # 180 degree turn. Coordinates are top-down sheet millimetres.
    x = placement["x"] + margin
    y = placement["y"] + margin
    if placement["flipped"]:
        x0 = (x + 4) * diagram_scale
        x1 = x0
        y0 = (y + placement["height"] - 4) * diagram_scale
        y1 = (y + 4) * diagram_scale
        draw.line((x0, y0, x1, y1), fill=(40, 52, 58, 255), width=2)
        draw.polygon([(x1, y1), (x1 - 4, y1 + 7), (x1 + 4, y1 + 7)], fill=(40, 52, 58, 255))
        draw.text((x0 + 6, y0 - 6), "grain", fill=(40, 52, 58, 255), font=typeface)
        return
    x0 = (x + 4) * diagram_scale
    x1 = (x + placement["width"] - 4) * diagram_scale
    yline = (y + 4) * diagram_scale
    draw.line((x0, yline, x1, yline), fill=(40, 52, 58, 255), width=2)
    draw.polygon([(x1, yline), (x1 - 7, yline - 4), (x1 - 7, yline + 4)], fill=(40, 52, 58, 255))
    draw.text((x0, yline - 14), "grain", fill=(40, 52, 58, 255), font=typeface)


def render_source_overlay(plan, frame):
    """Original registered photograph with confirmed pane edges and numbers."""
    rgb = (np.clip(frame.linear, 0, 1) * 255).astype(np.uint8)
    image = Image.fromarray(rgb).convert("RGBA")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    extent, scale = frame.extent, frame.scale
    to_px_x, to_px_y = width / extent[0], height / extent[1]
    typeface = font(max(11, round(min(width, height) / 48)))
    included = {item["pane"] for item in plan["layout"]}
    for piece in plan["pieces"]:
        pts = [(p[0] / scale[0] * to_px_x, p[1] / scale[1] * to_px_y) for p in piece["polygon"]]
        color = (94, 210, 212, 255) if piece["pane"] in included else (249, 179, 88, 255)
        draw.line([*pts, pts[0]], fill=color, width=2, joint="curve")
        centre = np.asarray(piece["polygon"]).mean(axis=0)
        draw.text(
            (centre[0] / scale[0] * to_px_x - 6, centre[1] / scale[1] * to_px_y - 8),
            str(piece["pane"]), fill=INK, font=typeface, stroke_width=2, stroke_fill=(20, 28, 32, 255),
        )
    return image


# --------------------------------------------------------------------------- #
# Plan assembly
# --------------------------------------------------------------------------- #

def _frame_snapshot(frame):
    # Only scalar facts are persisted; the source photograph itself is retained
    # as the rendered source_overlay blob, so later captures cannot move it.
    return {
        "frame": {
            "extent": list(frame.extent),
            "scale": list(frame.scale),
        },
        "photo": {
            "id": frame.photo["id"],
            "display_name": frame.photo["name"],
            "original_digest": frame.photo["source_digest"],
        },
    }


def create_cutting_plan(store, payload):
    spec = cutting_spec(payload)
    census = store.result(spec["census_result_id"])
    tile, frame, area, options = census_frame(store, census)
    cells, scale = pane_components(frame, area, options)
    pieces, excluded = build_pieces(cells, scale, spec["grain_angles"], census["metrics"]["rows"])
    if not pieces:
        raise InputError("No fully bounded panes are available; every census cell touches an unphotographed edge.")
    field_w = spec["sheet_width_mm"] - 2 * spec["margin_mm"]
    field_h = spec["sheet_height_mm"] - 2 * spec["margin_mm"]
    kerf = spec["kerf_mm"]
    groups = []
    if spec["strategy"] == "economy":
        sheets = pack_economy(pieces, field_w, field_h, kerf)
        if sheets is None:
            raise InputError("The bounded panes do not fit the stock field at the given margin and kerf.")
        strategy_note = (
            "Economy layout minimises the number of whole standard sheets. Original neighbouring "
            "panes may be split across separate sheets, so matching the grain across a came line "
            "needs the two cut pieces to be compared by hand."
        )
    else:
        sheets, groups = pack_neighbor(pieces, cells, field_w, field_h, kerf)
        if sheets is None:
            raise InputError("The neighbouring groups do not fit the stock field at the given margin and kerf.")
        strategy_note = (
            "Neighbour layout keeps each group of panes that shared a came line in the original window "
            "on one stock sheet, preserving on-sheet grain alignment for matching across the joint. "
            "It can consume more whole sheets than the economy layout."
        )

    layout = flatten_layout(sheets)
    piece_records = [{
        "pane": piece.pane,
        "angle": piece.angle,
        "width": round(piece.width, 3),
        "height": round(piece.height, 3),
        "anchor": [round(piece.anchor[0], 3), round(piece.anchor[1], 3)],
        "rotate": [round(piece.rotate[0], 6), round(piece.rotate[1], 6)],
        "polygon": piece.polygon,
    } for piece in pieces]

    plan = {
        "tile_id": tile["id"],
        "title": spec["title"],
        "strategy": spec["strategy"],
        "status": "draft",
        "spec": {key: value for key, value in spec.items() if key != "grain_angles"},
        "grain_angles": spec["grain_angles"],
        "census": {
            "result_id": census["id"],
            "created_at": census["created_at"],
            "analyzer_version": census["provenance"]["analyzer_version"],
            "photo_id": census["provenance"]["photo"]["id"],
            "photo_digest": census["provenance"]["photo"]["original_digest"],
            "options": census["parameters"]["options"],
        },
        "pieces": piece_records,
        "excluded": excluded,
        "adjacency_groups": groups,
        "layout": layout,
        "sheet_count": len(sheets),
        "strategy_note": strategy_note,
        "snapshot": _frame_snapshot(frame),
        "queries": [],
    }
    plan["diagrams"] = [
        stored_preview(render_sheet(plan, index), store.blobs)
        for index in range(1, plan["sheet_count"] + 1)
    ]
    plan["source_overlay"] = stored_preview(render_source_overlay(plan, frame), store.blobs)
    return store.save_cutting_plan(plan)


def adopt_cutting_plan(store, plan_id, payload):
    data = dictionary(payload, "Adoption")
    version = integer(data.get("version"), "Version", lower=1)
    plan = store.cutting_plan(plan_id)
    if plan["status"] == "adopted":
        raise ConflictError("This plan is already the adopted version; its layout is frozen.")
    existing = store.adopted_cutting_plan(plan["tile_id"])
    if existing is not None:
        raise ConflictError(
            f"Retire adopted plan '{existing['title']}' before another plan can be sent out."
        )
    return store.adopt_cutting_plan(plan_id, version)


def retire_cutting_plan(store, plan_id, payload):
    data = dictionary(payload, "Retirement")
    version = integer(data.get("version"), "Version", lower=1)
    return store.retire_cutting_plan(plan_id, version)


def record_point_query(store, plan_id, payload):
    data = dictionary(payload, "Point query")
    plan = store.cutting_plan(plan_id)
    result = locate_point(plan, data.get("pane"), data.get("point"))
    return store.append_cutting_query(plan_id, plan["version"], result)
