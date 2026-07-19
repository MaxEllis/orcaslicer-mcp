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


# --- regression: enums populated with emplace_back (not just push_back) ---
EMPLACE_FIXTURE = r'''
    def = this->add("overhang_fan_threshold", coEnums);
    def->label = L("Overhang cooling activation threshold");
    def->enum_values.emplace_back("0%");
    def->enum_values.emplace_back("25%");
    def->enum_values.emplace_back("95%");
    def->enum_labels.emplace_back("0%");
    def->enum_labels.emplace_back("25%");
    def->enum_labels.emplace_back("95%");
    def->set_default_value(new ConfigOptionEnumsGeneric{ 0 });

    def = this->add("curr_bed_type", coEnum);
    def->label = L("Bed type");
    def->enum_values.emplace_back("Cool Plate");
    def->enum_values.emplace_back("Textured PEI Plate");
    def->enum_labels.emplace_back(L("Smooth Cool Plate"));
    def->enum_labels.emplace_back(L("Textured PEI Plate"));
    def->set_default_value(new ConfigOptionEnum<BedType>(btPC));
'''

def test_parses_emplace_back_enum_values():
    # emplace_back is used for ~29 enum-value lines in PrintConfig.cpp (bed type,
    # overhang fan threshold, brim type, ...). Must be parsed just like push_back.
    settings, _ = parse_print_config(EMPLACE_FIXTURE)
    s = settings["overhang_fan_threshold"]
    assert s["type"] == "coEnums"
    assert s["enum_values"] == ["0%", "25%", "95%"]
    assert s["enum_labels"] == ["0%", "25%", "95%"]

def test_parses_emplace_back_enum_with_L_macro_labels():
    settings, _ = parse_print_config(EMPLACE_FIXTURE)
    s = settings["curr_bed_type"]
    assert s["enum_values"] == ["Cool Plate", "Textured PEI Plate"]
    assert s["enum_labels"] == ["Smooth Cool Plate", "Textured PEI Plate"]


# --- regression: enum lists copied from a def alias (def->enum_values = X->enum_values) ---
COPY_FIXTURE = r'''
    auto def_top_fill_pattern = def = this->add("top_surface_pattern", coEnum);
    def->label = L("Top surface pattern");
    def->enum_values.push_back("monotonic");
    def->enum_values.push_back("concentric");
    def->enum_labels.push_back(L("Monotonic"));
    def->enum_labels.push_back(L("Concentric"));
    def->set_default_value(new ConfigOptionEnum<InfillPattern>(ipMonotonic));

    def = this->add("bottom_surface_pattern", coEnum);
    def->label = L("Bottom surface pattern");
    def->enum_values = def_top_fill_pattern->enum_values;
    def->enum_labels = def_top_fill_pattern->enum_labels;
    def->set_default_value(new ConfigOptionEnum<InfillPattern>(ipMonotonic));
'''

def test_resolves_enum_values_copied_from_alias():
    # bottom_surface_pattern / internal_solid_infill_pattern / infill_anchor_max copy
    # their enum lists from another def via C++ assignment, not push_back.
    settings, _ = parse_print_config(COPY_FIXTURE)
    assert settings["top_surface_pattern"]["enum_values"] == ["monotonic", "concentric"]
    b = settings["bottom_surface_pattern"]
    assert b["enum_values"] == ["monotonic", "concentric"]
    assert b["enum_labels"] == ["Monotonic", "Concentric"]
    # temp resolution fields must not leak into the record
    assert not any(k.startswith("_copy") for k in b)
