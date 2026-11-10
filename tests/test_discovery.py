"""Finding OrcaSlicer on this computer when no token is set (discovery.py, config.load_config)."""
import json
import os
import time

import pytest

from orcaslicer_mcp.config import load_config, NOT_FOUND
from orcaslicer_mcp import discovery

TOKEN_A = "a" * 32   # made-up tokens
TOKEN_B = "b" * 32


@pytest.fixture
def datadir(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCA_DATADIR", str(tmp_path))
    monkeypatch.delenv("ORCA_API_TOKEN", raising=False)
    monkeypatch.delenv("ORCA_API_URL", raising=False)
    return tmp_path


def plugin_file(datadir, token=TOKEN_A, port=13130, folder="orcaslicer_mcp_plugin", product="orcaslicer-mcp-plugin",
                url=None, pid=None):
    d = datadir / "orca_plugins" / "plugin_data" / folder
    d.mkdir(parents=True, exist_ok=True)
    f = d / "connection.json"
    f.write_text(json.dumps({"product": product, "url": url or f"http://127.0.0.1:{port}", "token": token,
                             "pid": os.getpid() if pid is None else pid}))
    return f


DEAD_PID = 2 ** 22 + 12345  # above Linux's pid_max default: no such process


def build_conf(datadir, token=TOKEN_B, enabled="true", port="13131"):
    app = {"remote_api_enabled": enabled, "remote_api_token": token, "remote_api_port": port}
    body = json.dumps({"app": app}, indent=4)
    (datadir / "OrcaSlicer.conf").write_text(body + "\n# MD5 checksum 0123456789abcdef0123456789abcdef\n")


def test_the_plugins_file_gives_address_and_token(datadir):
    plugin_file(datadir, port=13200)
    cfg = load_config()
    assert cfg.token == TOKEN_A and cfg.base_url == "http://127.0.0.1:13200" and cfg.source == "plugin"


def test_the_builds_settings_give_address_and_token(datadir):
    build_conf(datadir)
    cfg = load_config()
    assert cfg.token == TOKEN_B and cfg.base_url == "http://127.0.0.1:13131" and cfg.source == "build"


def test_the_build_with_its_remote_api_off_is_not_used(datadir):
    build_conf(datadir, enabled="false")
    with pytest.raises(RuntimeError, match="wasn't found"):
        load_config()


def test_a_running_plugin_comes_before_the_build(datadir):
    build_conf(datadir)
    plugin_file(datadir)
    assert load_config().source == "plugin"


def test_an_explicit_token_always_wins(datadir, monkeypatch):
    plugin_file(datadir)
    monkeypatch.setenv("ORCA_API_TOKEN", "explicit")
    cfg = load_config()
    assert cfg.token == "explicit" and cfg.source == "env" and cfg.base_url == "http://127.0.0.1:13130"


def test_never_for_an_address_on_another_computer(datadir, monkeypatch):
    plugin_file(datadir)
    monkeypatch.setenv("ORCA_API_URL", "http://192.0.2.5:13130")
    with pytest.raises(RuntimeError) as e:
        load_config()
    assert str(e.value) == NOT_FOUND


def test_an_explicit_local_address_on_the_found_port_is_kept(datadir, monkeypatch):
    plugin_file(datadir, port=13999)
    monkeypatch.setenv("ORCA_API_URL", "http://localhost:13999/")
    cfg = load_config()
    assert cfg.base_url == "http://localhost:13999" and cfg.token == TOKEN_A


def test_a_local_address_on_a_port_nobody_uses_follows_the_token(datadir, monkeypatch):
    """The Claude extension used to send http://127.0.0.1:13130 always: with the plugin moved to another port, the
    token goes where it belongs, not to whatever answers on 13130."""
    plugin_file(datadir, port=13135)
    monkeypatch.setenv("ORCA_API_URL", "http://127.0.0.1:13130")
    cfg = load_config()
    assert cfg.base_url == "http://127.0.0.1:13135" and cfg.token == TOKEN_A


def test_a_local_address_picks_the_source_on_its_port(datadir, monkeypatch):
    plugin_file(datadir, port=13130)
    build_conf(datadir, port="13131")
    monkeypatch.setenv("ORCA_API_URL", "http://127.0.0.1:13131")
    cfg = load_config()
    assert cfg.source == "build" and cfg.token == TOKEN_B


def test_a_plugin_installed_from_orcacloud_is_found(datadir):
    """OrcaSlicer keeps a subscribed plugin's data in plugin_data/_subscribed/<user>/<plugin>/."""
    plugin_file(datadir, folder="_subscribed/user-1/16d116a6bc41", port=13140)
    cfg = load_config()
    assert cfg.source == "plugin" and cfg.base_url == "http://127.0.0.1:13140"


def test_a_file_naming_another_computer_is_never_used(datadir):
    """Another plugin could write a look-alike file: its address must be this computer, or the token and every reply
    would go elsewhere."""
    plugin_file(datadir, url="http://attacker.example:13130", folder="other_plugin")
    with pytest.raises(RuntimeError, match="wasn't found"):
        load_config()
    build_conf(datadir)
    assert load_config().source == "build"


def test_a_file_left_by_a_crashed_orcaslicer_comes_after_the_build(datadir):
    plugin_file(datadir, pid=DEAD_PID)
    build_conf(datadir)
    assert load_config().source == "build"


def test_a_left_file_is_still_used_when_nothing_else_is_there(datadir):
    plugin_file(datadir, pid=DEAD_PID, port=13150)
    assert load_config().base_url == "http://127.0.0.1:13150"


def test_pid_alive():
    assert discovery.pid_alive(os.getpid()) is True
    assert discovery.pid_alive(DEAD_PID) is False
    assert discovery.pid_alive(None) is None and discovery.pid_alive("12") is None and discovery.pid_alive(True) is None


def test_the_newest_plugin_file_wins_and_strangers_are_ignored(datadir):
    old = plugin_file(datadir, token=TOKEN_B, folder="orcaslicer_mcp_plugin")
    os.utime(old, (time.time() - 100, time.time() - 100))
    plugin_file(datadir, token=TOKEN_A, folder="orcaslicer_mcp_plugin_any")
    plugin_file(datadir, token="c" * 32, folder="someone_else", product="another-plugin")
    assert load_config().token == TOKEN_A


def test_broken_files_are_skipped(datadir):
    d = datadir / "orca_plugins" / "plugin_data" / "x"
    d.mkdir(parents=True)
    (d / "connection.json").write_text("{not json")
    (datadir / "OrcaSlicer.conf").write_text("garbage")
    with pytest.raises(RuntimeError, match="wasn't found"):
        load_config()


def test_nothing_found_names_the_way_out(datadir):
    with pytest.raises(RuntimeError) as e:
        load_config()
    for words in ("ORCA_API_TOKEN", "plugin", "ORCA_DATADIR", "ORCA_API_URL"):
        assert words in str(e.value)


def test_standard_folders_per_os(monkeypatch, tmp_path):
    monkeypatch.delenv("ORCA_DATADIR", raising=False)
    monkeypatch.setattr(discovery.sys, "platform", "darwin")
    assert discovery.datadir_candidates()[0].parts[-3:] == ("Library", "Application Support", "OrcaSlicer")
    monkeypatch.setattr(discovery.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert discovery.datadir_candidates() == [tmp_path / "OrcaSlicer"]
    monkeypatch.setattr(discovery.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert discovery.datadir_candidates()[0] == tmp_path / "OrcaSlicer"


def test_an_unexpanded_extension_placeholder_counts_as_not_set(datadir, monkeypatch):
    plugin_file(datadir)
    monkeypatch.setenv("ORCA_API_TOKEN", "${user_config.api_token}")
    monkeypatch.setenv("ORCA_API_URL", "${user_config.api_url}")
    cfg = load_config()
    assert cfg.source == "plugin" and cfg.token == TOKEN_A and cfg.base_url == "http://127.0.0.1:13130"
