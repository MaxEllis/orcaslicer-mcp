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
