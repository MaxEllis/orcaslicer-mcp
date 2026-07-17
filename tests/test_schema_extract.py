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

    //def = this->add("adaptive_layer_height", coBool);
    //def->label = L("Adaptive layer height");
    //def->tooltip = L("Disabled feature, "
    //    "commented out.");
    //def->set_default_value(new ConfigOptionBool(0));

    def = this->add("branch_angle", coFloat);
    // TRN PrintSettings: #lmFIXME
    def->label = L("Branch Diameter Angle");
    def->tooltip = L("Angle of the branches.");
    def->set_default_value(new ConfigOptionFloat(5));

    def = this->add("min_wall", coPercent);
    def->label = L("Minimum wall width");
    def->tooltip = L("Width of the wall.");
    def->set_default_value(new ConfigOptionPercent(85));

    // Declare retract values for filament profile, overriding the printer's extruder profile.
    for (auto& opt_key : filament_override_keys) {
        def = this->add_nullable(opt_key, it_opt->second.type);
        def->label      = it_opt->second.label;
        def->tooltip    = it_opt->second.tooltip;
        def->min        = it_opt->second.min;
    }

    def = this->add("after_loop", coBool);
    def->label = L("After Loop");

    def = this->add("printable_area", coPoints);
    def->label = L("Printable area");

    def = this->add("compatible_printers", coStrings);
'''

def test_parses_float_setting_with_semicolon_in_tooltip():
    settings, _ = parse_print_config(FIXTURE)
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

def test_commented_out_definition_is_skipped():
    settings, _ = parse_print_config(FIXTURE)
    assert "adaptive_layer_height" not in settings

def test_interior_comment_line_does_not_hide_label():
    # a `// TRN` comment between add() and def->label must not swallow the label
    settings, _ = parse_print_config(FIXTURE)
    s = settings["branch_angle"]
    assert s["label"] == "Branch Diameter Angle"
    assert s["tooltip"] == "Angle of the branches."

def test_add_nullable_loop_does_not_corrupt_preceding_setting():
    # def->label = it_opt->second.label (no string literal) must NOT null min_wall's label
    settings, unparsed = parse_print_config(FIXTURE)
    s = settings["min_wall"]
    assert s["label"] == "Minimum wall width"
    assert s["tooltip"] == "Width of the wall."
    assert "min_wall" not in unparsed
    # the dynamic add_nullable(opt_key,...) must not create a phantom setting
    assert "opt_key" not in settings
    # and the real setting after the loop still parses
    assert settings["after_loop"]["label"] == "After Loop"

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
    # layer_height, sparse_infill_pattern, branch_angle, min_wall, after_loop,
    # printable_area, compatible_printers  (adaptive_layer_height commented out)
    assert set(settings) == {"layer_height", "sparse_infill_pattern", "branch_angle",
                             "min_wall", "after_loop", "printable_area", "compatible_printers"}
