"""An OrcaSlicer that opens on its Home tab: stock 2.5 creates the plugin's panel only once Prepare shows, so until
then nothing answers. The assistant is told to ask for the Prepare tab (503 panel_not_open, which the connector passes
through word for word) instead of the 504 ui_timeout's "a dialog may be open", which kept it looking for a dialog."""
import pytest

import fake_orca
from conftest import call, mod

S = fake_orca.STATE


@pytest.fixture
def on_home(monkeypatch):
    """OrcaSlicer still on its Home tab when the plugin loads (request before `plugin`)."""
    monkeypatch.setattr(fake_orca, "PLATER_SHOWN", False)
    monkeypatch.setattr(mod, "UI_TIMEOUT_S", 0.5)


def test_panel_not_open_until_prepare_shows(on_home, plugin):
    status, body = call(plugin, "GET", "/api/v1/status")
    assert status == 503
    assert body["error"] == "panel_not_open"
    assert "Prepare" in body["detail"]
    fake_orca.show_plater()
    status, body = call(plugin, "GET", "/api/v1/status")
    assert status == 200, body
    assert body["presets"]["printer"]


def test_a_page_that_answered_before_still_times_out_as_busy(monkeypatch, plugin):
    monkeypatch.setattr(mod, "UI_TIMEOUT_S", 0.5)
    assert call(plugin, "GET", "/api/v1/status")[0] == 200
    S.page_silent = True  # OrcaSlicer's UI thread busy: the page that answered before goes quiet
    status, body = call(plugin, "GET", "/api/v1/status")
    assert (status, body["error"]) == (504, "ui_timeout")


def test_connector_passes_the_reason_through(on_home, plugin):
    errors = pytest.importorskip("orcaslicer_mcp.errors")
    status, body = call(plugin, "GET", "/api/v1/status")
    message = str(errors.error_from_status(status, body))
    assert message.startswith("panel_not_open: ")
    assert "Prepare" in message
