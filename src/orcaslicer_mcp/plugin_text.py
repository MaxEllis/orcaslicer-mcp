"""What the assistant says when the OrcaSlicer MCP plugin (stock OrcaSlicer) cannot do something.

A tester on the plugin asked for a slice and got one paragraph that named no action and offered
only "install the build", the thing she came to avoid (usability test 2026-11-09, M9). Each
refusal now names what failed, gives the way to do it by hand in OrcaSlicer first, and mentions
the OrcaSlicer MCP build second.
"""
from __future__ import annotations

BUILD_URL = "https://github.com/MaxEllis/OrcaSlicer/releases"

_SLICE = ("ask the user to press Slice plate (top right of the OrcaSlicer window) and tell you "
          "the print time and filament use it shows")
_GCODE = ("ask the user to slice in OrcaSlicer and use Export G-code (or Print) themselves")
_OBJECTS = "ask the user to do it on the plate in the OrcaSlicer window"
_PLATES = "ask the user to use the plate buttons in the OrcaSlicer window"
_PROJECTS = ("ask the user to use OrcaSlicer's File menu (New Project, Open Project, "
             "Save Project)")
_PER_OBJECT = ("ask the user to right-click the object in OrcaSlicer and change it there")

# tool name -> (what failed, the way by hand)
_ACTIONS: dict[str, tuple[str, str]] = {
    "slice": ("Slicing", _SLICE),
    "slice_and_wait": ("Slicing", _SLICE),
    "apply_and_slice": ("Slicing", _SLICE),
    "compare_slices": ("Comparing slices", "change one setting at a time and " + _SLICE),
    "compare_settings": ("Comparing slices", "change one setting at a time and " + _SLICE),
    "get_slice_status": ("Reading slice results", _SLICE),
    "get_slice_warnings": ("Reading slice warnings",
                           "ask the user to slice in OrcaSlicer and read you any warnings it shows"),
    "get_slice_breakdown": ("Reading the slice breakdown",
                            "ask the user to slice and read the per-feature times from OrcaSlicer's "
                            "preview legend"),
    "cancel_slice": ("Stopping a slice", "ask the user to stop it in the OrcaSlicer window"),
    "get_gcode": ("Reading the G-code", _GCODE),
    "save_gcode": ("Saving the G-code", _GCODE),
    "describe_plate": ("Describing the sliced plate",
                       "ask the user to slice in OrcaSlicer and describe the preview, or share a "
                       "screenshot"),
    "render_plate": ("Rendering a picture of the plate",
                     "ask the user to look at the plate in the OrcaSlicer window and describe it, "
                     "or share a screenshot"),
    "arrange_plate": ("Arranging the plate",
                      "ask the user to press Arrange all objects in OrcaSlicer's toolbar above the plate"),
    "auto_orient": ("Auto-orienting objects",
                    "ask the user to press Auto orient in OrcaSlicer's toolbar above the plate"),
    "get_job_status": ("Checking OrcaSlicer's background jobs",
                       "ask the user whether OrcaSlicer still shows a progress bar"),
    "load_model": ("Loading a model",
                   "ask the user to drag the file onto the plate in OrcaSlicer, or use File > Import"),
    "transform_object": ("Moving, rotating or scaling an object", _OBJECTS),
    "duplicate_object": ("Copying an object", _OBJECTS),
    "delete_object": ("Removing an object", _OBJECTS),
    "check_placement": ("Checking placement", _OBJECTS),
    "set_object_config": ("Per-object settings", _PER_OBJECT),
    "set_layer_height": ("Variable layer height", _PER_OBJECT),
    "set_height_range": ("Height-range settings", _PER_OBJECT),
    "list_plates": ("Listing plates", _PLATES),
    "add_plate": ("Adding a plate", _PLATES),
    "select_plate": ("Switching plates", _PLATES),
    "delete_plate": ("Deleting a plate", _PLATES),
    "move_object_to_plate": ("Moving an object to another plate", _PLATES),
    "save_project": ("Saving the project", _PROJECTS),
    "open_project": ("Opening a project", _PROJECTS),
    "open_recent_project": ("Opening a recent project", "ask the user to use File > Recent Projects in OrcaSlicer"),
    "new_project": ("Starting a new project", _PROJECTS),
    "list_recent_projects": ("Listing recent projects", "ask the user to look at File > Recent Projects in OrcaSlicer"),
    "forget_recent_project": ("Editing the recent projects list", "ask the user to do it in OrcaSlicer's File > Recent Projects"),
    "browse_projects": ("Listing project files", "ask the user to look in the folder themselves"),
    "move_project_file": ("Moving a project file", "ask the user to move or rename the file themselves"),
    "delete_project_file": ("Deleting a project file", "ask the user to delete the file themselves"),
    "save_preset": ("Saving a preset",
                    "ask the user to click the save icon beside the preset name in OrcaSlicer's "
                    "settings sidebar"),
    "edit_preset": ("Saving a preset",
                    "change the settings with set_config, then ask the user to click the save icon "
                    "beside the preset name in OrcaSlicer's settings sidebar"),
    "rename_preset": ("Renaming a preset",
                      "ask the user to save it under the new name in OrcaSlicer (the save icon beside "
                      "the preset name) and delete the old one"),
    "delete_preset": ("Deleting a preset",
                      "ask the user to select it in OrcaSlicer and use the delete icon beside its name"),
    "watch_events": ("Watching OrcaSlicer's events", "ask the user what changed in the window"),
}


def plugin_refusal(tool: str | None) -> dict:
    """The reply for a tool the plugin cannot serve."""
    action, by_hand = _ACTIONS.get(tool or "", ("That", "ask the user to do it in the OrcaSlicer window"))
    text = (f"not_available_in_plugin: {action} isn't available through the OrcaSlicer MCP plugin "
            f"(stock OrcaSlicer). By hand: {by_hand}. For the assistant to do this itself, the user "
            f"would need the OrcaSlicer MCP build instead: {BUILD_URL}")
    return {"error": text, "action": tool, "by_hand": by_hand[0].upper() + by_hand[1:] + "."}
