"""A stand-in for OrcaSlicer 2.5's embedded `orca` module, just wide enough for the plugin.

It mimics the parts of the host contract that matter for correctness:
  * model/preset reads are only legal on the "UI thread" (a dedicated thread here); a read from any
    other thread raises, so a read that bypasses the pump fails the tests;
  * dock-panel posts reach the page asynchronously and the page's replies run on_message on the UI
    thread, like CallAfter + WebView;
  * preset transactions stage with set() and apply on commit(), all or nothing, rejecting unknown keys.
All values are invented.
"""
from __future__ import annotations

import queue
import threading
import types

# ------------------------------------------------------------------ UI thread


class _UiThread:
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True, name="fake-ui")
        self.thread.start()

    def _run(self):
        while True:
            fn = self.q.get()
            try:
                fn()
            except Exception as e:  # mirrors wx swallowing callback errors into the log
                print("fake-ui callback error:", e)

    def call_after(self, fn):
        self.q.put(fn)

    def run_blocking(self, fn):
        if threading.current_thread() is self.thread:
            return fn()
        box, done = {}, threading.Event()

        def job():
            try:
                box["r"] = fn()
            except BaseException as e:
                box["e"] = e
            done.set()
        self.q.put(job)
        done.wait()
        if "e" in box:
            raise box["e"]
        return box["r"]


UI = _UiThread()


def _ui_only():
    if threading.current_thread() is not UI.thread:
        raise RuntimeError("fake host: read off the UI thread")


# ------------------------------------------------------------------ data


class BoundingBox:
    def __init__(self, mn, mx):
        self.min, self.max, self.defined = tuple(mn), tuple(mx), True

    @property
    def size(self):
        return tuple(b - a for a, b in zip(self.min, self.max))


class Preset:
    def __init__(self, name, config, system=False, visible=True, default=False):
        self.name, self._saved = name, dict(config)
        self._cfg = dict(config)
        self.is_system, self.is_visible, self.is_default = system, visible, default
        self.is_compatible = True

    @property
    def is_dirty(self):
        return self._cfg != self._saved

    def config_keys(self):
        _ui_only()
        return list(self._cfg)

    def config_value(self, key):
        _ui_only()
        return self._cfg.get(key)


class _SavedView:
    def __init__(self, preset):
        self._p = preset
        self.name, self.is_system, self.is_visible, self.is_default = (
            preset.name, preset.is_system, preset.is_visible, preset.is_default)
        self.is_dirty = False

    def config_keys(self):
        _ui_only()
        return list(self._p._saved)

    def config_value(self, key):
        _ui_only()
        return self._p._saved.get(key)


class Collection:
    def __init__(self, presets, selected):
        self.presets, self.sel = presets, selected

    def _by(self, name):
        return next((p for p in self.presets if p.name == name), None)

    def size(self):
        _ui_only()
        return len(self.presets)

    def preset(self, i):
        _ui_only()
        return self.presets[i]

    def find_preset(self, name):
        _ui_only()
        return self._by(name)

    def selected_preset_name(self):
        _ui_only()
        return self.sel

    def edited_preset(self):
        _ui_only()
        return self._by(self.sel)

    def selected_preset(self):
        _ui_only()
        return _SavedView(self._by(self.sel))


class Bundle:
    def __init__(self):
        self.prints = Collection([
            Preset("-- default --", {}, default=True),
            Preset("0.20mm Standard @Fake", {"layer_height": "0.2", "wall_loops": "2", "outer_wall_speed": "35",
                                             "compatible_printers": "", "post_process": "", "notes": ""},
                   system=True),
            Preset("My Fast", {"layer_height": "0.3", "wall_loops": "2", "outer_wall_speed": "35", "compatible_printers": "",
                               "post_process": ""}),
        ], "0.20mm Standard @Fake")
        self.filaments = Collection([
            Preset("Fake PLA", {"nozzle_temperature": "210", "filament_start_gcode": "; start",
                                "compatible_printers": ""}, system=True),
            Preset("Fake PETG", {"nozzle_temperature": "240", "filament_start_gcode": "; start",
                                 "compatible_printers": ""}),
        ], "Fake PLA")
        self.printers = Collection([
            Preset("Fake Printer 0.4", {"nozzle_diameter": "0.4", "machine_start_gcode": "G28",
                                        "print_host": "http://user:pass@printer.invalid",
                                        "printhost_apikey": "not-a-real-key", "printer_model": "Fake"},
                   system=False),
        ], "Fake Printer 0.4")
        self.filament_slots = ["Fake PLA"]
        # project_config's keys as full_config shows them (applied over the print and printer presets)
        self.project = {"curr_bed_type": "Textured PEI Plate", "flush_volumes_matrix": "0", "wipe_tower_x": "15",
                        "wipe_tower_y": "220", "flush_multiplier": "1"}

    def current_filament_preset_names(self):
        _ui_only()
        return list(self.filament_slots)

    def _full(self):
        cfg = {}
        for c in (self.prints, self.printers):
            cfg.update(c._by(c.sel)._cfg)
        cfg.update(self.project)
        cfg.update(self.filaments._by(self.filaments.sel)._cfg)
        return cfg

    def full_config_keys(self):
        _ui_only()
        return list(self._full())

    def full_config_value(self, key):
        _ui_only()
        return self._full().get(key)


class Instance:
    _next_id = 500

    def __init__(self, offset, bbox):
        self._o, self._bb = offset, bbox
        self._rot, self._scale, self._mirror = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (1.0, 1.0, 1.0)
        Instance._next_id += 1
        self._id = Instance._next_id

    def id(self):
        _ui_only()
        return self._id

    def offset(self):
        _ui_only()
        return self._o

    def rotation(self):
        return self._rot

    def scaling_factor(self):
        return self._scale

    def mirror(self):
        return self._mirror

    def bounding_box(self):
        _ui_only()
        return self._bb


class Volume:
    """A part, modifier, negative volume or support enforcer/blocker of an object."""
    _next = 900

    def __init__(self, kind="ModelPart", offset=(0.0, 0.0, 0.0), config=None):
        Volume._next += 1
        self._id, self.kind, self._offset = Volume._next, kind, offset
        self.config = dict(config or {})
        self.support_painted = False

    def id(self):
        _ui_only()
        return self._id

    def type(self):
        _ui_only()
        return "ModelVolumeType." + self.kind

    def offset(self):
        return self._offset

    def rotation(self):
        return (0.0, 0.0, 0.0)

    def scaling_factor(self):
        return (1.0, 1.0, 1.0)

    def mirror(self):
        return (1.0, 1.0, 1.0)

    def facets_count(self):
        return 12

    def config_keys(self):
        return list(self.config)

    def config_value(self, key):
        return self.config.get(key)

    def is_fdm_support_painted(self):
        return self.support_painted

    def is_seam_painted(self):
        return False

    def is_mm_painted(self):
        return False

    def is_fuzzy_skin_painted(self):
        return False


class ModelObject:
    def __init__(self, oid, name, bbox):
        self._id, self.name, self._bb = oid, name, bbox
        self._insts = [Instance((10.0, 20.0, 5.0), bbox)]
        self.config = {"wall_loops": "3"}  # the object's own settings
        self._volumes = [Volume()]
        self.custom_layering = False

    def volumes(self):
        _ui_only()
        return list(self._volumes)

    def id(self):
        _ui_only()
        return self._id

    def bounding_box(self):
        _ui_only()
        return self._bb

    def instance_count(self):
        return len(self._insts)

    def instances(self):
        _ui_only()
        return list(self._insts)

    def config_keys(self):
        _ui_only()
        return list(self.config)

    def config_value(self, key):
        _ui_only()
        return self.config.get(key)

    def has_custom_layering(self):
        return self.custom_layering


class Model:
    def __init__(self):
        self.objs = [ModelObject(101, "cube", BoundingBox((0, 0, 0), (20, 20, 20)))]
        self.plate = 0  # the plate OrcaSlicer shows

    def current_plate_index(self):
        _ui_only()
        return self.plate

    def objects(self):
        _ui_only()
        return list(self.objs)

    def object_count(self):
        _ui_only()
        return len(self.objs)


class Plater:
    def __init__(self, model):
        self._model = model
        self.path = ""

    def model(self):
        return self._model

    def project_path(self):
        _ui_only()
        return self.path

    def is_project_dirty(self):
        return False

    def is_presets_dirty(self):
        return False


# ------------------------------------------------------------------ transactions


class Transaction:
    def __init__(self, preset):
        self._p, self._staged = preset, {}

    def set(self, key, value):
        if key in self._staged:
            raise ValueError("duplicate " + key)
        self._staged[key] = value

    def commit(self):
        def apply():
            for k in self._staged:
                if k not in self._p._cfg:
                    raise ValueError(f"Invalid key '{k}' for this preset")
                if self._staged[k] == "bad":
                    raise ValueError(f"Invalid value for '{k}'")
            # OrcaSlicer deserializes the text and serializes it back C-style: real line breaks read back escaped.
            self._p._cfg.update({k: v.replace("\r", "\\r").replace("\n", "\\n") if isinstance(v, str) else v
                                 for k, v in self._staged.items()})
        UI.run_blocking(apply)
        return types.SimpleNamespace(keys=list(self._staged))


class Editor:
    def __init__(self, attr):
        self.attr = attr

    def begin_update(self, *args):
        if threading.current_thread() is UI.thread:
            raise RuntimeError("begin_update from the UI thread")
        bundle = STATE.bundle
        coll = getattr(bundle, self.attr)
        if self.attr == "filaments":
            slot, name = (args + ("",))[:2]
            name = name or bundle.filament_slots[slot - 1]
        else:
            name = args[0] if args else ""
            name = name or coll.sel
        if STATE.decline_selection and name != coll.sel:
            raise RuntimeError("The user cancelled the preset selection")
        if coll._by(name) is None:
            raise ValueError("unknown preset " + name)
        old = coll._by(coll.sel) if self.attr != "filaments" else coll._by(bundle.filament_slots[slot - 1])
        if old is not None and old.name != name and old.is_dirty:
            # OrcaSlicer asks what to do with the unsaved edits: transfer them, save them, or discard them.
            edits = {k: v for k, v in old._cfg.items() if old._saved.get(k) != v}
            if STATE.switch_mode == "transfer":
                coll._by(name)._cfg.update(edits)
                old._cfg = dict(old._saved)
            elif STATE.switch_mode == "save":
                old._saved = dict(old._cfg)
            elif STATE.switch_mode == "discard":
                old._cfg = dict(old._saved)
        coll.sel = name
        if self.attr == "filaments":
            bundle.filament_slots[slot - 1] = name
        return Transaction(coll._by(name))


# ------------------------------------------------------------------ ui


class Panel:
    def __init__(self, on_message, on_close):
        self.on_message, self.on_close = on_message, on_close
        self.open, self.shown, self.posts = True, True, []
        UI.call_after(lambda: self.on_message({"action": "ready"}))

    def post(self, data):
        self.posts.append(data)
        if STATE.page_silent:
            return
        if data.get("t") == "run":
            UI.call_after(lambda: self.on_message({"t": "run", "id": data["id"]}))
        elif data.get("type") == "history":  # the shared page asks for live values after each history
            UI.call_after(lambda: self.on_message({"action": "live"}))

    def is_open(self):
        return self.open

    def show(self):
        self.shown = True

    def close(self):
        if self.open:
            self.open = False
            self.on_close()


class State:
    def __init__(self):
        self.reset()

    def reset(self, storage=""):
        self.bundle = Bundle()
        self.model = Model()
        self.plater = Plater(self.model)
        self.storage = storage
        self.panels: list[Panel] = []
        self.notifications: list[tuple] = []
        self.links: list[tuple] = []  # (text, hyper_text, on_click) of notifications with a link
        self.page_silent = False
        self.decline_selection = False
        self.switch_mode = "keep"  # what the person picks for unsaved edits on a preset switch (keep = old behaviour)
        self.permissions = None
        self.config = {}


STATE = State()


def _push_notification(lvl, text, hyper_text="", on_click=None):
    STATE.notifications.append((lvl, text))
    if hyper_text:
        STATE.links.append((text, hyper_text, on_click))


def _create_dock_panel(html, title="", width=0, height=0, on_message=None, on_close=None, dock="right"):
    p = UI.run_blocking(lambda: Panel(on_message, on_close))
    STATE.panels.append(p)
    return p


# ------------------------------------------------------------------ module


def build_module():
    orca = types.ModuleType("orca")

    class ScriptPluginCapabilityBase:
        def __init__(self):
            pass

        def get_config(self):  # the stored config as JSON text, "{}" when never saved
            import json
            return json.dumps(STATE.config)

        def save_config(self, text):
            import json
            STATE.config = json.loads(text)
            return True

    class ExecutionResult:
        def __init__(self, ok, msg):
            self.ok, self.message = ok, msg

        @staticmethod
        def success(msg=""):
            return ExecutionResult(True, msg)

        @staticmethod
        def failure(code, msg=""):
            return ExecutionResult(False, msg)

    orca.script = types.SimpleNamespace(ScriptPluginCapabilityBase=ScriptPluginCapabilityBase)
    orca.base = type("base", (), {})
    orca.plugin = lambda cls: cls
    orca.ExecutionResult = ExecutionResult
    orca.LifecycleEvent = types.SimpleNamespace(**{n: types.SimpleNamespace(name=n) for n in (
        "NewProject", "ProjectOpened", "ProjectAfterSave", "ProjectClosed", "PresetSelected", "SliceStarted",
        "GCodeExportFinished", "SlicingJobComplete", "PlateSelected", "ObjectAdded", "ObjectDeleted",
        "ObjectTransformed", "ObjectChanged", "PlateCreated", "PlateDeleted")})
    orca.LifecycleEvtCode = types.SimpleNamespace(**{n: types.SimpleNamespace(name=n) for n in ("Ok", "Error", "Warn")})
    orca.PluginResult = types.SimpleNamespace(RecoverableError=1)
    orca.registered = []
    orca.register_capability = orca.registered.append
    orca.request_permissions = lambda **kw: setattr(STATE, "permissions", kw)

    level = types.SimpleNamespace(WarningNotificationLevel="warn", RegularNotificationLevel="info")
    ui = types.SimpleNamespace(
        create_dock_panel=_create_dock_panel,
        push_notification=_push_notification,
        NotificationLevel=level,
    )
    preset = types.SimpleNamespace(process=Editor("prints"), printer=Editor("printers"),
                                   filament=Editor("filaments"))
    orca.host = types.SimpleNamespace(
        preset_bundle=lambda: STATE.bundle,
        plater=lambda: STATE.plater,
        model=lambda: STATE.model,
        app_info=lambda: (_ui_only(), {"version": "2.5.0"})[1],
        plugin=types.SimpleNamespace(storage=lambda: STATE.storage),
        ui=ui,
        preset=preset,
    )
    return orca
