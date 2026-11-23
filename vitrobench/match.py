"""Adopt one replacement glass piece from two candidates photographed under two lamps."""
import numpy as np

from .art import annotated_regions, magnitude_overlay, stored_preview
from .color import delta_e, float_list, linear_lab, swatch
from .geometry import active_region, region_domain, specimen_frame
from .model import InputError, match_fields
from .photos import luminance


LIGHT_ROLES = ("cool", "warm")
LIGHT_LABELS = {"cool": "Cool white", "warm": "Warm white"}
MIN_COMMON_PIXELS = 80
MIN_CLEAR_PIXELS = 40
MIN_CLEAR_LUMA = 0.04
MIN_SUPPORT_PIXELS = 48
WORST_CASE_TIE = 0.4
ADOPTION_RULE = (
    "Adopt the qualified candidate with the smaller worst-case ΔE across the two lamps. "
    "When the worst-case values tie within 0.4 ΔE, adopt the candidate closer to the original under warm light."
)


def light_photo_id(tile, role):
    registered = {tile["plane"]["photo_id"], *tile["registrations"]} if tile.get("plane") else set()
    choices = [photo["id"] for photo in tile["photos"] if photo["role"] == role and photo["id"] in registered]
    label = LIGHT_LABELS[role]
    if not choices:
        raise InputError(f"'{tile['name']}' needs a registered {label} capture before it can join a match.")
    if len(choices) > 1:
        raise InputError(f"'{tile['name']}' has several registered {label} captures; keep only one.")
    return choices[0]


def clear_reading(frame, mask):
    support = mask & frame.valid & ~frame.clipped
    count = int(np.count_nonzero(support))
    if count < MIN_CLEAR_PIXELS:
        raise InputError("The clear glass reference needs at least 40 supported, unclipped pixels in each lamp.")
    colors = frame.linear[support]
    median = np.median(colors, axis=0)
    luma = float(np.median(luminance(colors)))
    if luma < MIN_CLEAR_LUMA:
        raise InputError("The clear glass reference is too dark to normalize its lamp.")
    return {"median_linear_rgb": float_list(median, 7), "luma": round(luma, 6), "pixels": count}


def piece_reading(store, tile, spec):
    region = active_region(tile, spec["region_id"])
    if region is None:
        raise InputError("Mark the comparison position on every piece.")
    clear = active_region(tile, spec["clear_region_id"])
    if clear is None or clear["purpose"] != "bare":
        raise InputError("Choose a region marked as clear glass for the same-exposure reference.")
    frames = {role: specimen_frame(store.blobs, tile, light_photo_id(tile, role)) for role in LIGHT_ROLES}
    extent = frames["cool"].extent
    domain = region_domain(extent, tile, region)
    clear_mask = region_domain(extent, tile, clear)
    domain_px = int(np.count_nonzero(domain))
    visible = {role: domain & frame.valid & ~frame.clipped for role, frame in frames.items()}
    common = visible["cool"] & visible["warm"]
    common_px = int(np.count_nonzero(common))
    lights = {}
    for role, frame in frames.items():
        photo = frame.photo
        entry = {
            "photo": {
                "id": photo["id"],
                "name": photo["name"],
                "role": role,
                "source_digest": photo["source_digest"],
                "working_digest": photo["working_digest"],
            },
            "clear": clear_reading(frame, clear_mask),
            "visible_px": int(np.count_nonzero(visible[role])),
            "visible_ratio": round(np.count_nonzero(visible[role]) / domain_px, 4) if domain_px else 0,
        }
        if common_px >= MIN_COMMON_PIXELS:
            normalized = frame.linear[common] / np.asarray(entry["clear"]["median_linear_rgb"], dtype=np.float32)
            entry["median_lab"] = float_list(np.median(linear_lab(normalized), axis=0), 4)
            entry["swatch"] = swatch(np.median(normalized, axis=0))
        lights[role] = entry
    reading = {
        "tile_id": tile["id"],
        "tile_name": tile["name"],
        "tile_version": tile["version"],
        "plane": dict(tile["plane"]),
        "region": region,
        "clear_region": clear,
        "lights": lights,
        "domain_px": domain_px,
        "common_px": common_px,
        "common_ratio": round(common_px / domain_px, 4) if domain_px else 0,
    }
    return reading, frames


def grade_candidate(reading, original, minimum):
    if reading["common_px"] < MIN_COMMON_PIXELS:
        return {"qualified": False, "note": "Too few pixels are clearly visible under both lamps.",
                "delta_e": None, "worst_delta_e": None}
    if reading["common_ratio"] < minimum:
        return {"qualified": False,
                "note": f"Common visible share {reading['common_ratio'] * 100:.1f}% is below the required {minimum * 100:.1f}%.",
                "delta_e": None, "worst_delta_e": None}
    deltas = {}
    for role in LIGHT_ROLES:
        observed = np.asarray(reading["lights"][role]["median_lab"])
        approved = np.asarray(original["lights"][role]["median_lab"])
        deltas[role] = round(float(delta_e(observed, approved)), 3)
    return {"qualified": True, "note": None, "delta_e": deltas, "worst_delta_e": max(deltas.values())}


def decide(candidates, minimum):
    qualified = [index for index, item in enumerate(candidates) if item["qualified"]]
    if not qualified:
        return {
            "adopted": None, "adopted_tile_id": None, "adopted_name": None,
            "explanation": "Neither candidate reaches the minimum common visible ratio, so no replacement is "
                           "adopted. Add captures that cover the marked position under both lamps or lower the "
                           "required ratio.",
        }
    if len(qualified) == 1:
        index = qualified[0]
        chosen = candidates[index]
        return {
            "adopted": index, "adopted_tile_id": chosen["tile_id"], "adopted_name": chosen["tile_name"],
            "explanation": f"Only {chosen['tile_name']} reaches the minimum common visible ratio "
                           f"({chosen['common_ratio'] * 100:.1f}% ≥ {minimum * 100:.1f}%), so it is the single "
                           "qualified choice.",
        }
    first, second = candidates
    worst = [first["worst_delta_e"], second["worst_delta_e"]]
    if abs(worst[0] - worst[1]) <= WORST_CASE_TIE:
        index = 0 if first["delta_e"]["warm"] <= second["delta_e"]["warm"] else 1
        chosen, other = candidates[index], candidates[1 - index]
        note = (f"Both candidates qualify and their worst-case color differences tie within {WORST_CASE_TIE} ΔE "
                f"({worst[0]} vs {worst[1]}). The tie goes to {chosen['tile_name']}, the closer warm-lamp match "
                f"(ΔE {chosen['delta_e']['warm']} vs {other['delta_e']['warm']}).")
    else:
        index = 0 if worst[0] < worst[1] else 1
        chosen, other = candidates[index], candidates[1 - index]
        note = (f"Both candidates qualify. {chosen['tile_name']} is adopted because its worst-case color "
                f"difference across the two lamps (ΔE {chosen['worst_delta_e']}) is smaller than "
                f"{other['tile_name']}'s (ΔE {other['worst_delta_e']}).")
        if other["delta_e"]["warm"] < chosen["delta_e"]["warm"]:
            note += (f" {other['tile_name']} sits closer to the original under the warm lamp "
                     f"(ΔE {other['delta_e']['warm']} vs {chosen['delta_e']['warm']}), but the adoption rule "
                     "limits the largest mismatch in any recorded lamp so the repair stays consistent whether "
                     "the panel is viewed under cool or warm light; warm-lamp closeness decides only near-ties.")
    return {"adopted": index, "adopted_tile_id": chosen["tile_id"], "adopted_name": chosen["tile_name"],
            "explanation": note}


def create_match(store, payload):
    fields = match_fields(payload)
    minimum = fields["minimum_common_visible"]
    specs = [fields["original"], *fields["candidates"]]
    positions = {(spec["tile_id"], spec["region_id"]) for spec in specs}
    if len(positions) != len(specs):
        raise InputError("Mark a different comparison position for each piece.")
    tiles = {}
    readings, frames = [], []
    for spec in specs:
        if spec["tile_id"] not in tiles:
            tiles[spec["tile_id"]] = store.tile(spec["tile_id"])
        reading, piece_frames = piece_reading(store, tiles[spec["tile_id"]], spec)
        readings.append(reading)
        frames.append(piece_frames)
    original = readings[0]
    if original["common_px"] < MIN_COMMON_PIXELS or original["common_ratio"] < minimum:
        raise InputError(
            "The original piece falls below the minimum common visible ratio "
            f"({original['common_ratio'] * 100:.1f}% < {minimum * 100:.1f}%), so the match has no usable "
            "reference evidence."
        )
    candidates = []
    for reading in readings[1:]:
        candidates.append({**reading, **grade_candidate(reading, original, minimum)})
    record = {
        "title": fields["title"],
        "notes": fields["notes"],
        "minimum_common_visible": minimum,
        "rule": ADOPTION_RULE,
        "original": original,
        "candidates": candidates,
        "decision": decide(candidates, minimum),
        "sheets": [
            stored_preview(annotated_regions(piece_frames["cool"], [reading["region"], reading["clear_region"]]), store.blobs)
            for piece_frames, reading in zip(frames, readings)
        ],
    }
    return store.save_match(record)


def summarize_match(record):
    return {
        "id": record["id"],
        "title": record["title"],
        "created_at": record["created_at"],
        "original_tile_id": record["original"]["tile_id"],
        "original_name": record["original"]["tile_name"],
        "candidate_names": [item["tile_name"] for item in record["candidates"]],
        "adopted_tile_id": record["decision"]["adopted_tile_id"],
        "adopted_name": record["decision"]["adopted_name"],
        "qualified": sum(1 for item in record["candidates"] if item["qualified"]),
    }


def matched_frames(store, tile, record):
    if record["original"]["tile_id"] != tile["id"]:
        raise InputError("This match record belongs to a different original specimen.")
    plane = tile.get("plane")
    frozen = record["original"]["plane"]
    if (
        not plane
        or plane["photo_id"] != frozen["photo_id"]
        or plane["corners"] != frozen["corners"]
        or plane["size"] != frozen["size"]
    ):
        raise InputError("The live specimen plane no longer matches the plane frozen in this match record.")
    frames = {}
    for role in LIGHT_ROLES:
        wanted = record["original"]["lights"][role]["photo"]
        photo = next((item for item in tile["photos"] if item["id"] == wanted["id"]), None)
        if photo is None:
            raise InputError(f"The {LIGHT_LABELS[role]} capture frozen in this match was removed from the specimen.")
        if photo["source_digest"] != wanted["source_digest"]:
            raise InputError(f"The {LIGHT_LABELS[role]} capture on the specimen differs from the frozen match basis.")
        frames[role] = specimen_frame(store.blobs, tile, wanted["id"])
    return frames


def transmission_by_match(frames, record, area, options):
    """Relative transmission per lamp against the clear-glass references frozen in the match."""
    floor = options["floor"]
    if not np.any(area):
        raise InputError("The selected area has no pixels to measure.")
    per_light = {}
    for role, frame in frames.items():
        reference = record["original"]["lights"][role]["clear"]["luma"]
        light = luminance(frame.linear)
        support = area & frame.valid & ~frame.clipped
        usable = np.count_nonzero(support) >= MIN_SUPPORT_PIXELS and reference > floor * 2
        resolved = support & (light >= floor) if usable else np.zeros_like(support)
        density = np.zeros_like(light)
        if np.any(resolved):
            density[resolved] = np.log10(reference / light[resolved])
        per_light[role] = {"support": support, "resolved": resolved, "usable": usable, "density": density}
    if not any(np.any(data["resolved"]) for data in per_light.values()):
        raise InputError("Neither lamp covers enough of the selected area above the sensor floor.")
    xs = np.nonzero(area.any(axis=0))[0]
    rows = []
    for index, columns in enumerate(np.array_split(xs, min(options["cells"], len(xs))), 1):
        span = slice(int(columns[0]), int(columns[-1]) + 1)
        row = {"station": index, "columns": [span.start, span.stop]}
        for role in LIGHT_ROLES:
            data = per_light[role]
            if not data["usable"] or not np.count_nonzero(data["support"][:, span]):
                row[role] = None
                continue
            values = data["density"][:, span][data["resolved"][:, span]]
            row[role] = {
                "resolvable_px": len(values),
                "below_floor_px": int(np.count_nonzero(data["support"][:, span] & ~data["resolved"][:, span])),
                "unobserved_px": int(np.count_nonzero(area[:, span] & ~data["support"][:, span])),
                "median_density": round(float(np.median(values)), 5) if len(values) else None,
                "median_transmission_pct": round(float(np.median(np.power(10.0, -values))) * 100, 3) if len(values) else None,
            }
        rows.append(row)
    lights = {}
    for role in LIGHT_ROLES:
        data = per_light[role]
        values = data["density"][data["resolved"]]
        frozen = record["original"]["lights"][role]
        lights[role] = {
            "photo": frozen["photo"]["name"],
            "reference_luma": frozen["clear"]["luma"],
            "reference_pixels": frozen["clear"]["pixels"],
            "support_px": int(np.count_nonzero(data["support"])),
            "resolvable_px": int(np.count_nonzero(data["resolved"])),
            "median_density": round(float(np.median(values)), 5) if len(values) else None,
            "median_transmission_pct": round(float(np.median(np.power(10.0, -values))) * 100, 3) if len(values) else None,
        }
    cool = per_light["cool"]
    shown = np.maximum(cool["density"], 0)
    image = magnitude_overlay(frames["cool"], shown, cool["resolved"], 1.25, unknown=area & ~cool["resolved"])
    metrics = {
        "match_id": record["id"],
        "match_title": record["title"],
        "adopted_name": record["decision"]["adopted_name"],
        "lights": lights,
        "rows": rows,
        "note": "Relative transmitted intensity per lamp against the clear-glass references frozen in the match "
                "record. Empty cells mark positions the corresponding capture did not cover.",
    }
    return metrics, image, None
