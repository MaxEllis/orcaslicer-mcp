from orcaslicer_mcp import plate_describe as pd

MINI = """; header
; EXECUTABLE_BLOCK_START
M83
;LAYER_CHANGE
;Z:0.4
;HEIGHT:0.4
;TYPE:Outer wall
G1 X10 Y10 F3000
G1 X20 Y10 E1
G1 X20 Y20 E1
G1 X10 Y20 E1
G1 X10 Y10 E1
;LAYER_CHANGE
;Z:1
;HEIGHT:0.6
;TYPE:Inner wall
G1 X12 Y12 E0.5
; EXECUTABLE_BLOCK_END
; CONFIG_BLOCK_START
; layer_height = 0.6
; printable_area = 0x0,300x0,300x300,0x300
; seam_position = back
; CONFIG_BLOCK_END
"""


def test_parse_layers_and_config_from_minimal_gcode():
    p = pd.parse_gcode(MINI)
    assert [round(z, 1) for z, _ in p.layers] == [0.4, 1.0]
    assert [round(h, 1) for _, h in p.layers] == [0.4, 0.6]
    assert p.config["layer_height"] == "0.6"
    assert p.config["seam_position"] == "back"
    assert p.printable_area == [(0.0, 0.0), (300.0, 0.0), (300.0, 300.0), (0.0, 300.0)]
    assert p.per_object is False
    assert list(p.objects) == ["plate"]


def test_object_markers_split_objects_and_set_per_object():
    text = MINI.replace(";TYPE:Outer wall\n", "; printing object A.stl id:0 copy 0\n;TYPE:Outer wall\n") \
               .replace(";TYPE:Inner wall\n", "; stop printing object A.stl id:0 copy 0\n; printing object B.stl id:1 copy 0\n;TYPE:Inner wall\n")
    p = pd.parse_gcode(text)
    assert p.per_object is True
    assert sorted(p.objects) == ["A.stl", "B.stl"]
    assert p.copy_labels == {"A.stl": {0}, "B.stl": {0}}


def test_layer_with_no_extrusion_and_move_before_any_layer_are_harmless():
    text = MINI.replace(";TYPE:Inner wall\nG1 X12 Y12 E0.5\n", ";TYPE:Inner wall\nG1 X12 Y12 F3000\n")
    text = "G1 X5 Y5 E1\n" + text          # extrusion before the first ;LAYER_CHANGE is ignored
    p = pd.parse_gcode(text)
    assert len(p.layers) == 2
    assert 1 not in p.objects["plate"].layers          # no accumulator is created for an empty layer
    assert len(p.objects["plate"].layers[0].cells) > 0


def test_real_fixtures_parse(gcode_fixture):
    cube = pd.parse_gcode(gcode_fixture("cube20_flat"))
    assert len(cube.layers) == 34 and cube.per_object is True and list(cube.objects) == ["cube20.stl"]
    body = pd.parse_gcode(gcode_fixture("body4_corner_x3_support"))
    assert len(body.layers) == 194 and list(body.objects) == ["Body4.stl"]
    assert body.copy_labels == {"Body4.stl": {0}}  # Orca labels every duplicate copy 0 (spec: per-copy limitation)
    assert body.config["support_type"] == "tree(auto)" and body.config["seam_position"] == "back"


def test_seam_recorded_on_unlabelled_plate_when_no_markers():
    """When no object markers are present, seams are recorded on the "plate" object."""
    text = MINI.replace("G1 X10 Y10 E1\n;LAYER_CHANGE", "G1 X10 Y10 E1\n;WIPE_START\n;LAYER_CHANGE")
    p = pd.parse_gcode(text)
    assert len(p.objects["plate"].seams) == 1
    assert p.objects["plate"].seams[0] == (0, 10.0, 10.0)


def test_seam_recorded_on_named_object_not_plate_when_markers_present():
    """When object markers are present, seams are recorded on the named object, not the transient "plate"."""
    text = MINI.replace(";TYPE:Outer wall\n", "; printing object A.stl id:0 copy 0\n;TYPE:Outer wall\n") \
               .replace(";TYPE:Inner wall\n", "; stop printing object A.stl id:0 copy 0\n;TYPE:Inner wall\n") \
               .replace("G1 X10 Y10 E1\n;LAYER_CHANGE", "G1 X10 Y10 E1\n;WIPE_START\n;LAYER_CHANGE")
    p = pd.parse_gcode(text)
    assert len(p.objects["A.stl"].seams) == 1
    assert p.objects["A.stl"].seams[0] == (0, 10.0, 10.0)
    # "plate" should have no seams (only layers from the Inner wall section after the marker)
    assert len(p.objects["plate"].seams) == 0


def _square(x0, y0, n):
    return {(x0 + i, y0 + j) for i in range(n) for j in range(n)}


def test_islands_splits_components_and_drops_stragglers():
    cells = _square(0, 0, 10) | _square(20, 20, 5) | {(40, 40)}
    out = pd.islands(cells)
    assert [i["area_mm2"] for i in out] == [100, 25]           # the 1-cell straggler is dropped (min_cells=3)
    assert out[0]["bbox"] == [0, 0, 10, 10]                      # bbox is in mm: cell max + 1
    assert pd.islands(cells, min_cells=1)[-1] == {"area_mm2": 1, "bbox": [40, 40, 41, 41]}


def test_islands_are_8_connected():
    cells = {(0, 0), (1, 1), (2, 2)}                            # diagonal chain
    assert len(pd.islands(cells, min_cells=1)) == 1


def test_contact_classes():
    o = pd.ObjectAcc("t")
    o.layer(0).cells = _square(0, 0, 4)          # 16 cells on the plate
    o.layer(5).cells = _square(0, 0, 20)         # widest layer 400 cells
    c = pd.contact(o)
    assert c["footprint_area_mm2"] == 16 and c["max_layer_area_mm2"] == 400
    assert c["contact_ratio"] == 0.04 and c["class"] == "edge_or_corner"
    o.layer(0).cells = _square(0, 0, 12)         # 144/400 = 0.36
    assert pd.contact(o)["class"] == "tilted"
    o.layer(0).cells = _square(0, 0, 20)
    assert pd.contact(o)["class"] == "flat" and pd.contact(o)["contact_ratio"] == 1.0


def test_contact_with_no_first_layer_is_zero_not_error():
    o = pd.ObjectAcc("t")
    o.layer(3).cells = _square(0, 0, 5)
    c = pd.contact(o)
    assert c["footprint_area_mm2"] == 0 and c["contact_ratio"] == 0.0 and c["class"] == "edge_or_corner"


def test_overhang_bands_by_z():
    o = pd.ObjectAcc("t")
    layers = [(0.4, 0.4), (5.0, 0.6), (12.0, 0.6), (25.0, 0.6)]
    o.layer(0).wall_mm, o.layer(0).overhang_mm = 100.0, 30.0
    o.layer(1).wall_mm, o.layer(1).overhang_mm = 100.0, 0.0
    o.layer(2).wall_mm, o.layer(2).overhang_mm = 50.0, 10.0
    o.layer(3).wall_mm, o.layer(3).overhang_mm = 50.0, 0.0
    out = pd.overhang_bands(o, layers)
    assert out["total_mm"] == 40.0
    assert out["bands"] == [{"z0": 0, "z1": 10, "share": 0.15, "overhang_mm": 30.0},
                            {"z0": 10, "z1": 20, "share": 0.2, "overhang_mm": 10.0},
                            {"z0": 20, "z1": 30, "share": 0.0, "overhang_mm": 0.0}]


def test_real_fixture_grid_facts(gcode_fixture):
    cube = pd.parse_gcode(gcode_fixture("cube20_flat")).objects["cube20.stl"]
    c = pd.contact(cube)
    assert c["class"] == "flat" and c["contact_ratio"] == 1.0
    assert 380 <= c["footprint_area_mm2"] <= 420
    assert len(pd.islands(cube.layer(0).cells)) == 1
    body = pd.parse_gcode(gcode_fixture("body4_corner_x3_support")).objects["Body4.stl"]
    c = pd.contact(body)
    assert c["class"] == "edge_or_corner" and 0.05 <= c["contact_ratio"] <= 0.15
    assert len(pd.islands(body.layer(0).cells)) == 3       # three copies, stragglers filtered


def test_support_absent():
    o = pd.ObjectAcc("t")
    o.layer(0).cells = _square(0, 0, 5)
    out = pd.support(o, [(0.4, 0.4)])
    assert out == {"present": False, "z_range": None, "islands": [], "interface_zones": []}


def test_support_towers_and_interface_zones():
    o = pd.ObjectAcc("t")
    layers = [(0.4, 0.4), (1.0, 0.6), (1.6, 0.6), (2.2, 0.6)]
    o.layer(0).support_cells = _square(0, 0, 4) | _square(30, 30, 4)   # two towers stand here
    o.layer(1).support_cells = _square(0, 0, 4) | _square(30, 30, 4)
    o.layer(2).support_cells = _square(0, 0, 4)
    o.layer(2).interface_cells = _square(0, 0, 4)                        # interface touches the part at Z 1.6
    o.layer(3).support_cells = _square(30, 30, 4)
    o.layer(3).interface_cells = _square(30, 30, 4)
    out = pd.support(o, layers)
    assert out["present"] is True and out["z_range"] == [0.4, 2.2]
    assert [i["area_mm2"] for i in out["islands"]] == [16, 16]
    zones = out["interface_zones"]
    assert len(zones) == 2
    assert {"bbox": [0, 0, 4, 4], "z0": 1.6, "z1": 1.6, "area_mm2": 16} in zones
    assert {"bbox": [30, 30, 34, 34], "z0": 2.2, "z1": 2.2, "area_mm2": 16} in zones


def test_interface_zones_merge_adjacent_layers_over_the_same_spot():
    o = pd.ObjectAcc("t")
    layers = [(0.4, 0.4), (1.0, 0.6), (1.6, 0.6)]
    for i in range(3):
        o.layer(i).support_cells = _square(0, 0, 3)
        o.layer(i).interface_cells = _square(0, 0, 3)
    zones = pd.support(o, layers)["interface_zones"]
    assert zones == [{"bbox": [0, 0, 3, 3], "z0": 0.4, "z1": 1.6, "area_mm2": 9}]


def test_interface_zones_ignore_stragglers_and_merge_across_gap_layers():
    o = pd.ObjectAcc("t")
    layers = [(0.4, 0.4), (1.0, 0.6), (1.6, 0.6)]
    o.layer(0).support_cells = _square(0, 0, 4)
    o.layer(0).interface_cells = _square(0, 0, 4)
    o.layer(1).support_cells = _square(0, 0, 4) | {(30, 30)}    # support only here, plus a lone interface straggler
    o.layer(1).interface_cells = {(30, 30)}
    o.layer(2).support_cells = _square(0, 0, 4)
    o.layer(2).interface_cells = _square(0, 0, 4)
    zones = pd.support(o, layers)["interface_zones"]
    assert zones == [{"bbox": [0, 0, 4, 4], "z0": 0.4, "z1": 1.6, "area_mm2": 16}]


def test_seam_sides_relative_to_layer_centroid():
    o = pd.ObjectAcc("t")
    o.layer(0).cells = _square(0, 0, 20)          # centroid at (10, 10)
    o.seams = [(0, 10.0, 19.5), (0, 10.0, 19.0), (0, 19.5, 10.0)]   # two on +Y, one on +X
    out = pd.seam(o, "back")
    assert out["count"] == 3
    assert out["sides"] == {"+X": 0.33, "-X": 0.0, "+Y": 0.67, "-Y": 0.0}
    assert out["dominant"] == "+Y" and out["alignment"] == 0.67
    assert out["configured"] == "back" and out["agrees"] is True


def test_seam_scattered_and_unknown_config():
    o = pd.ObjectAcc("t")
    o.layer(0).cells = _square(0, 0, 20)
    o.seams = [(0, 10.0, 19.5), (0, 19.5, 10.0), (0, 0.5, 10.0), (0, 10.0, 0.5)]
    out = pd.seam(o, "random")
    assert out["alignment"] == 0.25 and out["dominant"] in ("+X", "-X", "+Y", "-Y")
    assert out["configured"] == "random" and out["agrees"] is None   # random/aligned/nearest have no side
    assert pd.seam(pd.ObjectAcc("empty"), "back") == {"count": 0, "sides": {"+X": 0.0, "-X": 0.0, "+Y": 0.0, "-Y": 0.0},
                                                       "dominant": None, "alignment": 0.0, "configured": "back", "agrees": None}


def test_real_fixture_support_and_seams(gcode_fixture):
    cube = pd.parse_gcode(gcode_fixture("cube20_flat"))
    s = pd.seam(cube.objects["cube20.stl"], cube.config.get("seam_position"))
    assert s["dominant"] == "+Y" and s["agrees"] is True
    assert pd.support(cube.objects["cube20.stl"], cube.layers)["present"] is False
    body = pd.parse_gcode(gcode_fixture("body4_corner_x3_support"))
    obj = body.objects["Body4.stl"]
    sup = pd.support(obj, body.layers)
    assert sup["present"] is True and sup["z_range"][0] < 1.0 and sup["z_range"][1] > 50.0
    assert len(sup["islands"]) >= 3 and 3 <= len(sup["interface_zones"]) <= 40
    assert all(z["area_mm2"] >= 3 for z in sup["interface_zones"])
    s = pd.seam(obj, body.config.get("seam_position"))
    assert s["count"] > 50 and s["dominant"] == "+Y" and s["alignment"] >= 0.7 and s["agrees"] is True


def test_describe_assembles_objects_copies_and_not_in_gcode(gcode_fixture):
    parsed = pd.parse_gcode(gcode_fixture("body4_corner_x3_support"))
    meta = [{"name": "Body4.stl", "instances": 3}, {"name": "ghost.stl", "instances": 1}]
    out = pd.describe(parsed, meta)
    assert out["per_object"] is True and out["not_in_gcode"] == ["ghost.stl"]
    assert out["plate"]["layer_count"] == 194 and out["plate"]["printable_area"][2] == [300.0, 300.0]
    assert out["plate"]["layer_height"] == "0.6" and 55 < out["plate"]["height_mm"] < 62
    (o,) = out["objects"]
    assert o["name"] == "Body4.stl" and o["copies"] == 3
    assert o["orientation"]["class"] == "edge_or_corner"
    assert len(o["footprint"]["islands"]) == 3 and o["footprint"]["bbox"][0] < o["footprint"]["bbox"][2]
    assert o["support"]["present"] is True and o["seam"]["dominant"] == "+Y"
    assert o["summary"] == out["summary"]          # one object -> the plate summary is its sentence


def test_describe_without_meta_defaults_copies_to_one_and_whole_plate_when_unlabelled():
    parsed = pd.parse_gcode(MINI)
    out = pd.describe(parsed, None)
    assert out["per_object"] is False
    (o,) = out["objects"]
    assert o["name"] == "plate" and o["copies"] == 1
    assert "label objects is off" in o["summary"]


def test_summarize_wording_edge_case_with_support_and_aligned_seam():
    desc = {"name": "Body4.stl", "copies": 3,
            "orientation": {"class": "edge_or_corner", "contact_ratio": 0.1},
            "footprint": {"area_mm2": 151, "max_layer_area_mm2": 1553, "bbox": [0, 0, 1, 1],
                          "islands": [{"area_mm2": 62, "bbox": []}, {"area_mm2": 44, "bbox": []}, {"area_mm2": 43, "bbox": []}]},
            "overhang": {"bands": [{"z0": 0, "z1": 10, "share": 0.31, "overhang_mm": 1500.0},
                                   {"z0": 10, "z1": 20, "share": 0.12, "overhang_mm": 500.0},
                                   {"z0": 20, "z1": 30, "share": 0.01, "overhang_mm": 10.0}], "total_mm": 2010.0},
            "support": {"present": True, "z_range": [0.4, 56.8], "islands": [{}] * 5,
                        "interface_zones": [{}] * 6},
            "seam": {"count": 90, "sides": {"+Y": 0.84, "-Y": 0.1, "+X": 0.02, "-X": 0.03}, "dominant": "+Y",
                     "alignment": 0.84, "configured": "back", "agrees": True}}
    s = pd.summarize_object(desc)
    assert s == ("Body4.stl (3 copies) stands on an edge or corner: first-layer contact is 10% of its widest "
                 "layer, in 3 islands of about 50 mm2 each. Overhang extrusions concentrate at Z 0 to 20 mm. "
                 "Support is present from Z 0.4 to 56.8 mm, standing in 5 places and touching the part in 6 zones. "
                 "Seams align on the +Y side (84%), matching seam_position=back.")


def test_summarize_wording_flat_no_support_scattered_seam_disagrees():
    desc = {"name": "cube20.stl", "copies": 1,
            "orientation": {"class": "flat", "contact_ratio": 1.0},
            "footprint": {"area_mm2": 400, "max_layer_area_mm2": 400, "bbox": [], "islands": [{"area_mm2": 400, "bbox": []}]},
            "overhang": {"bands": [{"z0": 0, "z1": 10, "share": 0.0, "overhang_mm": 0.0}], "total_mm": 0.0},
            "support": {"present": False, "z_range": None, "islands": [], "interface_zones": []},
            "seam": {"count": 33, "sides": {"+Y": 0.3, "-Y": 0.3, "+X": 0.2, "-X": 0.2}, "dominant": "+Y",
                     "alignment": 0.3, "configured": "back", "agrees": True}}
    s = pd.summarize_object(desc)
    assert s == ("cube20.stl lies flat: first-layer contact is 100% of its widest layer, in 1 island of about "
                 "400 mm2. No overhang extrusions. No support. Seams are scattered (largest share 30% on the +Y side), "
                 "although seam_position=back asks for one side.")
    desc["seam"] = {"count": 33, "sides": {"+Y": 0.1, "-Y": 0.8, "+X": 0.05, "-X": 0.05}, "dominant": "-Y",
                    "alignment": 0.8, "configured": "back", "agrees": False}
    assert pd.summarize_object(desc).endswith("Seams align on the -Y side (80%), which disagrees with seam_position=back.")
    desc["seam"] = {"count": 0, "sides": {"+Y": 0.0, "-Y": 0.0, "+X": 0.0, "-X": 0.0}, "dominant": None,
                    "alignment": 0.0, "configured": "back", "agrees": None}
    assert pd.summarize_object(desc).endswith("No seam points found.")
