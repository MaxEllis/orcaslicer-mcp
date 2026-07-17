from orcaslicer_mcp.schema_extract import parse_print_config

FIXTURE = r'''
    def           = this->add("layer_height", coFloat);
    def->label    = L("Layer height");
    def->category = L("Quality");
    def->tooltip  = L("Slicing height for each layer; smaller is more accurate.");
    def->sidetext = L("mm");
    def->min      = 0;
    def->mode     = comSimple;
    def->set_default_value(new ConfigOptionFloat(INITIAL_LAYER_HEIGHT));

    def = this->add("sparse_infill_pattern", coEnum);
    def->label = L("Sparse infill pattern");
    def->category = L("Strength");
    def->tooltip = L("Line pattern for internal "
                     "sparse infill.");
    def->enum_values.push_back("grid");
    def->enum_values.push_back("gyroid");
    def->enum_labels.push_back(L("Grid"));
    def->enum_labels.push_back(L("Gyroid"));
    def->set_default_value(new ConfigOptionEnum<InfillPattern>(ipGrid));

    def = this->add("printable_area", coPoints);
    def->label = L("Printable area");

    def = this->add("compatible_printers", coStrings);
'''

def test_parses_float_setting_with_semicolon_in_tooltip():
    settings, unparsed = parse_print_config(FIXTURE)
    s = settings["layer_height"]
    assert s["type"] == "coFloat"
    assert s["label"] == "Layer height"
    assert s["category"] == "Quality"
    assert s["tooltip"] == "Slicing height for each layer; smaller is more accurate."
    assert s["unit"] == "mm"
    assert s["min"] == 0
    assert s["mode"] == "comSimple"
    assert s["default"] == "INITIAL_LAYER_HEIGHT"
    assert s["enum_values"] is None

def test_parses_enum_and_joins_multiline_tooltip():
    settings, _ = parse_print_config(FIXTURE)
    s = settings["sparse_infill_pattern"]
    assert s["type"] == "coEnum"
    assert s["tooltip"] == "Line pattern for internal sparse infill."
    assert s["enum_values"] == ["grid", "gyroid"]
    assert s["enum_labels"] == ["Grid", "Gyroid"]
    assert s["default"] == "ipGrid"

def test_label_without_tooltip_is_not_unparsed():
    settings, unparsed = parse_print_config(FIXTURE)
    assert settings["printable_area"]["label"] == "Printable area"
    assert settings["printable_area"]["tooltip"] is None
    assert "printable_area" not in unparsed

def test_key_with_no_metadata_is_reported_unparsed():
    settings, unparsed = parse_print_config(FIXTURE)
    assert "compatible_printers" in settings
    assert "compatible_printers" in unparsed

def test_setting_count():
    settings, _ = parse_print_config(FIXTURE)
    assert len(settings) == 4
