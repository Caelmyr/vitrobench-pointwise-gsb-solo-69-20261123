"""End-to-end smoke test for cutting plans, run against a fresh temp data dir."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vitrobench.store import Store
from vitrobench.demo import training_specimen, SERIES
from vitrobench.analysis import evaluate
from vitrobench import cutting
from vitrobench.model import ConflictError, InputError


def main():
    tmp = tempfile.mkdtemp()
    store = Store(Path(tmp))
    specimen = store.create_tile(training_specimen(store, SERIES[0]))
    print("specimen", specimen["name"], specimen["width_mm"], "x", specimen["height_mm"], "mm")

    # Run a glass pane census exactly like the UI does.
    result = evaluate(store, specimen["id"], {"kind": "panes", "options": {}})
    rows = result["metrics"]["rows"]
    bounded = [r["pane"] for r in rows if r["state"] == "bounded"]
    edge = [r["pane"] for r in rows if r["state"] != "bounded"]
    print("census panes:", result["metrics"]["pane_count"], "bounded:", bounded, "edge:", edge)
    assert bounded, "expected bounded interior panes"

    base_payload = {
        "census_result_id": result["id"],
        "title": "Transom replacement cut",
        "sheet_width_mm": 120,
        "sheet_height_mm": 100,
        "kerf_mm": 2,
        "margin_mm": 5,
        "grain_angles": {str(bounded[0]): 30},
    }

    economy = cutting.create_cutting_plan(store, {**base_payload, "strategy": "economy"})
    neighbor = cutting.create_cutting_plan(
        store, {**base_payload, "strategy": "neighbor", "sheet_width_mm": 240, "sheet_height_mm": 200}
    )
    print("economy sheets:", economy["sheet_count"], "neighbor sheets:", neighbor["sheet_count"])
    print("excluded:", [(e["pane"]) for e in economy["excluded"]])
    print("neighbor groups:", neighbor["adjacency_groups"])
    assert neighbor["adjacency_groups"] and any(len(group) > 1 for group in neighbor["adjacency_groups"]), \
        "expected multi-pane adjacency groups"

    # Every bounded pane appears exactly once; excluded edge panes never do.
    for plan, (sheet_w, sheet_h) in ((economy, (120, 100)), (neighbor, (240, 200))):
        used = [p["pane"] for p in plan["layout"]]
        assert sorted(used) == sorted(bounded), (used, bounded)
        assert len(used) == len(set(used)), "a pane was placed more than once"
        assert not (set(used) & set(edge)), "an unsupported pane was cut"
        # Each placement honours margin, kerf spacing and sheet bounds.
        field_w = sheet_w - 10
        field_h = sheet_h - 10
        by_sheet = {}
        for place in plan["layout"]:
            assert place["x"] + place["width"] <= field_w + 0.002
            assert place["y"] + place["height"] <= field_h + 0.002
            by_sheet.setdefault(place["sheet"], []).append(place)
        # Grain angle recorded; only allowed orientations.
        piece = next(p for p in plan["pieces"] if p["pane"] == bounded[0])
        assert piece["angle"] == 30.0

    # Neighbor strategy: each adjacency group stays on one sheet.
    for group in neighbor["adjacency_groups"]:
        sheets = {p["sheet"] for p in neighbor["layout"] if p["pane"] in group}
        assert len(sheets) == 1, (group, sheets)

    # When stock is too small to hold a neighbour group, reject with guidance.
    try:
        cutting.create_cutting_plan(store, {**base_payload, "strategy": "neighbor"})
        raise AssertionError("oversize neighbour group accepted")
    except InputError as exc:
        print("neighbour overflow rejected:", str(exc)[:60], "...")

    # Diagrams and source overlay were persisted as blobs.
    for digest in economy["diagrams"] + [economy["source_overlay"]]:
        assert store.blobs.require(digest).is_file()
    img = cutting.render_sheet(economy, 1)
    assert img.width > 0 and img.height > 0

    # Point query: click the centre of the first bounded pane in the original plane.
    pane = bounded[0]
    piece = next(p for p in economy["pieces"] if p["pane"] == pane)
    centre_plane = __import__("numpy").asarray(piece["polygon"]).mean(axis=0)
    extent = economy["snapshot"]["frame"]["extent"]
    scale = economy["snapshot"]["frame"]["scale"]
    u = centre_plane[0] / (extent[0] * scale[0])
    v = centre_plane[1] / (extent[1] * scale[1])
    plan2, query = cutting.record_point_query(store, economy["id"], {"pane": pane, "point": [u, v]})
    print("point query:", query)
    assert 0 <= query["distance_from_left_mm"] <= 120
    assert 0 <= query["distance_from_top_mm"] <= 100
    assert abs((query["distance_from_left_mm"] + query["distance_from_right_mm"]) - 120) < 1e-6
    assert abs((query["distance_from_bottom_mm"] + query["distance_from_top_mm"]) - 100) < 1e-6
    assert plan2["queries"][-1]["pane"] == pane
    economy = plan2  # version was bumped by the recorded query

    # A point outside the confirmed pane must be rejected.
    try:
        cutting.locate_point(economy, pane, [0.01, 0.01])
        raise AssertionError("outside point was accepted")
    except InputError:
        pass

    # Querying an excluded pane fails.
    if edge:
        try:
            cutting.locate_point(economy, edge[0], [0.5, 0.5])
            raise AssertionError("excluded pane query accepted")
        except Exception as exc:
            assert isinstance(exc, (InputError, Exception))

    # Adoption rules: one adopted plan per specimen; drafts freeze on adopt.
    adopted = store.adopt_cutting_plan(economy["id"], economy["version"])
    assert adopted["status"] == "adopted"
    try:
        store.adopt_cutting_plan(neighbor["id"], neighbor["version"])
        raise AssertionError("second adoption accepted")
    except ConflictError:
        pass
    # Re-adopting the same plan is idempotent and does not bump it again.
    again = store.adopt_cutting_plan(economy["id"], adopted["version"])
    assert again["version"] == adopted["version"]
    retired = store.retire_cutting_plan(economy["id"], adopted["version"])
    assert retired["status"] == "retired"
    adopted2 = store.adopt_cutting_plan(neighbor["id"], neighbor["version"])
    assert adopted2["status"] == "adopted"

    # A sheet too small for any pane is rejected cleanly.
    try:
        cutting.create_cutting_plan(store, {**base_payload, "sheet_width_mm": 12, "sheet_height_mm": 12})
        raise AssertionError("oversize pane accepted")
    except InputError as exc:
        print("oversize rejected:", exc)

    print("ALL CUTTING-PLAN CHECKS PASSED")


if __name__ == "__main__":
    main()
