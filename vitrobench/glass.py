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

TRIAL_FIT_NOTE = (
    "A station fits only where both came edges were photographed and the measured width covers the stock plus both "
    "side margins. A station missing either edge breaks a run; widths are never bridged across it. When both stocks "
    "qualify, the tighter seat (less spare per side) is preferred over the longer continuous run, and exactly one "
    "stock and one interval are adopted."
)


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
    line_length = float(np.linalg.norm(along))
    return anchors, coordinates, offsets, "mm" if frame.scale else "px", start, delta, line_length


def reached_glass(dark, valid, middle, sense):
    for index in range(middle + sense, len(valid) if sense > 0 else -1, sense):
        if not valid[index]:
            return None, "missing support"
        if not dark[index]:
            return index, None
    return None, "edge outside band"


def fitting_runs(flags):
    runs = []
    index = 0
    while index < len(flags):
        if not flags[index]:
            index += 1
            continue
        stop = index
        while stop + 1 < len(flags) and flags[stop + 1]:
            stop += 1
        runs.append((index, stop))
        index = stop + 1
    return runs


def stock_evaluation(rows, key, stock_mm, margin_mm, spacing_mm, min_run_mm):
    required = stock_mm + 2 * margin_mm
    fitting = [row["state"] == "measured" and row["width"] >= required for row in rows]
    best = None
    for first, last in fitting_runs(fitting):
        spare = [rows[k]["width"] - stock_mm for k in range(first, last + 1)]
        run = {
            "first_station": first + 1,
            "last_station": last + 1,
            "stations": last - first + 1,
            "start_mm": round(first * spacing_mm, 3),
            "end_mm": round(last * spacing_mm, 3),
            "length_mm": round((last - first) * spacing_mm, 3),
            "mean_spare_mm": round(float(np.mean(spare)) / 2, 3),
        }
        if best is None or (run["length_mm"], -run["start_mm"]) > (best["length_mm"], -best["start_mm"]):
            best = run
    return {
        "key": key,
        "stock_mm": stock_mm,
        "required_mm": round(required, 3),
        "fitting_stations": sum(1 for flag in fitting if flag),
        "best_run": best,
        "qualified": bool(best and best["length_mm"] >= min_run_mm),
    }


def longest_run_mm(stock):
    return (stock["best_run"] or {}).get("length_mm", 0)


def decide_fit(stocks, min_run_mm):
    first, second = stocks
    if first["stock_mm"] == second["stock_mm"]:
        if first["qualified"]:
            run = first["best_run"]
            return first, (
                f"Stocks A and B are the same {first['stock_mm']:g} mm material. It reaches the {min_run_mm:g} mm "
                f"minimum over {run['length_mm']:g} mm between {run['start_mm']:g} and {run['end_mm']:g} mm of the "
                f"marked line. Adopted as stock A on that interval."
            )
        return None, (
            f"Stocks A and B are the same {first['stock_mm']:g} mm material; its longest continuous fit is "
            f"{longest_run_mm(first):g} mm, below the {min_run_mm:g} mm minimum. No trial fit adopted."
        )
    qualified = [stock for stock in stocks if stock["qualified"]]
    if not qualified:
        return None, (
            f"No trial fit adopted: the longest continuous fits are {longest_run_mm(first):g} mm (stock A, "
            f"{first['stock_mm']:g} mm) and {longest_run_mm(second):g} mm (stock B, {second['stock_mm']:g} mm), "
            f"both below the {min_run_mm:g} mm minimum."
        )
    if len(qualified) == 1:
        chosen = qualified[0]
        other = second if chosen is first else first
        run = chosen["best_run"]
        return chosen, (
            f"Only stock {chosen['key']} ({chosen['stock_mm']:g} mm) reaches the {min_run_mm:g} mm minimum "
            f"continuous fit: {run['length_mm']:g} mm between {run['start_mm']:g} and {run['end_mm']:g} mm of the "
            f"marked line. Stock {other['key']} ({other['stock_mm']:g} mm) manages {longest_run_mm(other):g} mm "
            f"at most. Adopted stock {chosen['key']} on that interval."
        )
    tighter, looser = sorted(
        qualified, key=lambda stock: (stock["best_run"]["mean_spare_mm"], -stock["stock_mm"], stock["key"])
    )
    t_run, l_run = tighter["best_run"], looser["best_run"]
    if t_run["length_mm"] >= l_run["length_mm"]:
        trade = (
            f"and also runs {t_run['length_mm']:g} mm continuously, at least as long as stock {looser['key']}'s "
            f"{l_run['length_mm']:g} mm. Adopted stock {tighter['key']} on {t_run['start_mm']:g}–{t_run['end_mm']:g} "
            f"mm of the marked line."
        )
    else:
        trade = (
            f"while stock {looser['key']} ({looser['stock_mm']:g} mm) runs longer continuously "
            f"({l_run['length_mm']:g} mm against {t_run['length_mm']:g} mm) but sits looser at "
            f"{l_run['mean_spare_mm']:g} mm per side. Adopted stock {tighter['key']} on {t_run['start_mm']:g}–"
            f"{t_run['end_mm']:g} mm of the marked line, preferring the tighter seat over the longer run."
        )
    return tighter, (
        f"Both stocks reach the {min_run_mm:g} mm minimum continuous fit. Stock {tighter['key']} "
        f"({tighter['stock_mm']:g} mm) seats tighter at {t_run['mean_spare_mm']:g} mm spare per side over its "
        f"best interval, {trade} Only this stock and interval are adopted."
    )


def trial_fit(rows, options, unit, line_length_mm):
    margin = options["margin_mm"]
    minimum = options["min_run_mm"]
    block = {
        "available": False,
        "unit": unit,
        "margin_mm": margin,
        "min_run_mm": minimum,
        "stocks": [],
        "adopted": None,
        "decision": None,
        "note": TRIAL_FIT_NOTE,
    }
    if unit != "mm":
        block["decision"] = "No trial fit: surveyed panel dimensions are missing, so widths were measured in pixels."
        return block
    spacing = line_length_mm / max(len(rows) - 1, 1)
    stocks = [
        stock_evaluation(rows, "A", options["stock_a_mm"], margin, spacing, minimum),
        stock_evaluation(rows, "B", options["stock_b_mm"], margin, spacing, minimum),
    ]
    chosen, decision = decide_fit(stocks, minimum)
    block.update(
        available=True,
        stocks=stocks,
        decision=decision,
        line_length_mm=round(line_length_mm, 3),
        station_spacing_mm=round(spacing, 3),
    )
    if chosen is not None:
        run = chosen["best_run"]
        block["adopted"] = {
            "stock": chosen["key"],
            "stock_mm": chosen["stock_mm"],
            "required_mm": chosen["required_mm"],
            "interval": {key: run[key] for key in ("start_mm", "end_mm", "length_mm", "first_station", "last_station")},
            "mean_spare_mm": run["mean_spare_mm"],
        }
    return block


def trial_fit_summary(fit):
    adopted = fit.get("adopted")
    if not adopted:
        return "No adopted trial fit."
    interval = adopted["interval"]
    return (
        f"Adopted stock {adopted['stock']} {adopted['stock_mm']:g} mm on {interval['start_mm']:g}–"
        f"{interval['end_mm']:g} mm of the marked line ({interval['length_mm']:g} mm continuous); "
        f"margins {fit['margin_mm']:g} mm per side."
    )


def draw_fit_interval(draw, start, delta, line_length, adopted):
    if line_length <= 0:
        return
    interval = adopted["interval"]
    near = start + (interval["start_mm"] / line_length) * delta
    far = start + (interval["end_mm"] / line_length) * delta
    normal = np.array([-delta[1], delta[0]]) / np.linalg.norm(delta)
    draw.line([tuple(near), tuple(far)], fill=PANE_COLOR, width=3)
    for point in (near, far):
        draw.line([tuple(point - normal * 6), tuple(point + normal * 6)], fill=PANE_COLOR, width=2)
    middle = (near + far) / 2
    label = f"fit {adopted['stock_mm']:g} mm · {interval['length_mm']:g} mm"
    draw.text((middle[0] + 5, middle[1] - 13), label, fill=PANE_COLOR, stroke_width=2, stroke_fill=(25, 31, 31))


def came_width(frame, area, linear, options):
    """Read both sides of the dark metal strip perpendicular to a marked line."""
    support = sampled_panel(frame, area)
    anchors, positions, offsets, unit, start, delta, line_length = station_coordinates(frame, options)
    uv = (positions[:, :, 1], positions[:, :, 0])
    valid = ndimage.map_coordinates(support.astype(np.uint8), uv, order=0, mode="constant").astype(bool)
    levels = ndimage.map_coordinates(luminance(linear), uv, order=1, mode="constant")
    metal = levels <= options["cutoff"]
    midpoint = len(offsets) // 2
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
        rows.append(row)
    fit = trial_fit(rows, options, unit, line_length)
    output = base_rgba(frame, linear)
    draw = ImageDraw.Draw(output)
    if fit["adopted"]:
        draw_fit_interval(draw, start, delta, line_length, fit["adopted"])
    for row, location in zip(rows, anchors):
        if row["edges_px"] is not None:
            mark_a, mark_b = row["edges_px"]
            draw.line([tuple(mark_a), tuple(mark_b)], fill=CAME_COLOR, width=2)
        else:
            x, y = location
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=UNCERTAIN_COLOR)
    measure = [row["width"] for row in rows if row["state"] == "measured"]
    return {
        "rows": rows,
        "length_unit": unit,
        "measured_stations": len(measure),
        "median_width": round(float(np.median(measure)), 4) if measure else None,
        "trial_fit": fit,
        "trial_fit_summary": trial_fit_summary(fit),
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
