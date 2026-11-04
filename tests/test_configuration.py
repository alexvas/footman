"""Phase 2 operator authority; all filesystem inputs are disposable."""

import dataclasses
import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.dispatcher_harness import read_bytes, run, write_bytes


def configuration_module():
    # Missing implementation is an assertion failure, not broken collection.
    try:
        return importlib.import_module("footman.configuration")
    except ModuleNotFoundError as error:
        pytest.fail(f"required configuration validation is absent: {error}")


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "repository").mkdir()
    return {
        "version": 1,
        "repository": "repository",
        "container_start_argv": [
            "docker",
            "run",
            "--detach=true",
            "--pull=never",
            "--privileged=true",
            "--",
            "{image_id}",
            "sleep",
            "infinity",
        ],
        "image_id": "d9e853e87e55",
        "command_argv": ["tool", "two words", "; $literal", "{phase}"],
        "working_directory": ".",
        "container_snapshot_directory": "/workspace",
        "snapshot_directory": "snapshots",
        "output_dir": "results/{phase}/{utc_datetime}",
        "timeout_seconds": 60,
        "cleanup_grace_seconds": 10,
    }


def save(path, settings):
    text = "\n".join(f"{key} = {json.dumps(value)}" for key, value in settings.items())
    write_bytes(path, text.encode())
    return path


def validate(settings, tmp_path):
    return configuration_module().validate_configuration(
        settings, tmp_path / "operator.toml"
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("version", None),
        ("version", 2),
        ("version", True),
        ("version", "1"),
        ("unexpected", "ignored?"),
        ("command_argv", []),
        ("command_argv", [""]),
        ("command_argv", "tool test"),
        ("command_argv", ["tool", 1]),
        ("command_argv", ["tool", "bad\x00arg"]),
        ("container_start_argv", []),
        ("container_start_argv", "docker run {snapshot_path}"),
        ("container_start_argv", ["docker", 1]),
        ("container_start_argv", [""]),
        ("container_start_argv", ["docker", "run"]),
        ("container_start_argv", ["docker", "{image_id!r}"]),
        ("container_start_argv", ["docker", "{image_id}", "{phase}"]),
        ("container_start_argv", ["docker", "{image_id"]),
        ("container_start_argv", ["docker", "{{image_id}}"]),
        ("output_dir", "results/{run_id}"),
        ("output_dir", "results/{unknown}"),
        ("output_dir", "results/{phase:>3}"),
        ("output_dir", "results/}"),
        ("output_dir", ""),
        ("output_dir", 3),
        ("repository", "missing"),
        ("repository", 42),
        ("snapshot_directory", ""),
        ("snapshot_directory", False),
        ("user", ""),
        ("user", 0),
        ("user", "bad\x00user"),
        ("working_directory", 42),
        ("working_directory", "/absolute"),
        ("working_directory", "../escape"),
        ("working_directory", "a/../../escape"),
        ("working_directory", ""),
        ("container_snapshot_directory", 42),
        ("container_snapshot_directory", "relative"),
        ("container_snapshot_directory", ""),
        ("timeout_seconds", 0),
        ("timeout_seconds", -1),
        ("timeout_seconds", 86401),
        ("timeout_seconds", True),
        ("timeout_seconds", "60"),
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", float("nan")),
        ("cleanup_grace_seconds", 0),
        ("cleanup_grace_seconds", -1),
        ("cleanup_grace_seconds", 301),
        ("cleanup_grace_seconds", False),
        ("cleanup_grace_seconds", float("inf")),
    ],
)
def test_invalid_configuration_table(settings, tmp_path, key, value):
    module = configuration_module()
    if value is None:
        settings.pop(key)
    else:
        settings[key] = value
    with pytest.raises(module.ConfigurationError, match=key):
        validate(settings, tmp_path)


@pytest.mark.parametrize(
    "key",
    [
        "repository",
        "container_start_argv",
        "command_argv",
        "working_directory",
        "container_snapshot_directory",
        "snapshot_directory",
        "output_dir",
        "timeout_seconds",
        "cleanup_grace_seconds",
    ],
)
def test_required_fields(settings, tmp_path, key):
    settings.pop(key)
    with pytest.raises(configuration_module().ConfigurationError, match=key):
        validate(settings, tmp_path)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("snapshot_directory", "repository/child"),
        ("snapshot_directory", "."),
        ("output_dir", "repository/results/{phase}"),
        ("output_dir", "snapshots/results/{utc_datetime}"),
        ("snapshot_directory", "results/child"),
        ("output_dir", "."),
    ],
)
def test_canonical_overlap(settings, tmp_path, key, value):
    settings[key] = value
    with pytest.raises(configuration_module().ConfigurationError, match="overlap"):
        validate(settings, tmp_path)


@pytest.mark.parametrize("key", ["snapshot_directory", "output_dir"])
def test_symlink_ancestors_cannot_hide_overlap(settings, tmp_path, key):
    (tmp_path / "alias").symlink_to(tmp_path / "repository", target_is_directory=True)
    settings[key] = "alias/new/child"
    with pytest.raises(configuration_module().ConfigurationError, match="overlap"):
        validate(settings, tmp_path)


def test_storage_symlink_overlap(settings, tmp_path):
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "snapshots", target_is_directory=True)
    settings["output_dir"] = "alias/new/{phase}"
    with pytest.raises(configuration_module().ConfigurationError, match="overlap"):
        validate(settings, tmp_path)


@pytest.mark.parametrize(
    "template", ["results/static", "results/{phase}", "results/{utc_datetime}"]
)
def test_valid_defaults_and_literal_argv(settings, tmp_path, template):
    settings["output_dir"] = template
    settings["working_directory"] = "sub/../nested"
    config = validate(settings, tmp_path)
    assert config.user == "root"
    assert config.repository == tmp_path / "repository"
    assert config.snapshot_directory == tmp_path / "snapshots"
    assert config.output_dir == str(tmp_path / template)
    assert config.working_directory == "nested"
    assert config.container_working_directory == "/workspace/nested"
    assert config.command_argv == tuple(settings["command_argv"])
    assert config.container_start_argv == tuple(settings["container_start_argv"])
    assert config.requires_phase == ("{phase}" in template)
    assert not (tmp_path / "snapshots").exists()
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize(("timeout", "grace"), [(0.001, 0.001), (86400, 300)])
def test_numeric_boundaries(settings, tmp_path, timeout, grace):
    settings.update(timeout_seconds=timeout, cleanup_grace_seconds=grace)
    config = validate(settings, tmp_path)
    assert config.timeout_seconds == timeout
    assert config.cleanup_grace_seconds == grace


def test_load_relative_to_file_not_caller(settings, tmp_path, monkeypatch):
    path = save(tmp_path / "operator.toml", settings)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    config = configuration_module().load_configuration(path)
    assert config.repository == tmp_path / "repository"
    assert config.container_working_directory == "/workspace"
    assert config.identity == validate(settings, tmp_path).identity


@pytest.mark.parametrize(
    "input_value",
    [
        {"phase": ""},
        {"phase": "."},
        {"phase": "a.b"},
        {"phase": "../x"},
        {"phase": "a/b"},
        {"phase": "a\\b"},
        {"phase": "a\n"},
        {"phase": 1},
        {"phase": None},
        {"phase": True},
        {"phase": "é"},
        {"phase": []},
        {"command_argv": ["evil"]},
        {"phase": "ok", "config": "other.toml"},
        [],
        "label",
        None,
        {"user": "other"},
        {"timeout_seconds": 1},
        {"image": "other"},
        {"output_dir": "/tmp/override"},
        {},
    ],
)
def test_invalid_requests_have_zero_downstream_calls(settings, tmp_path, input_value):
    module = configuration_module()
    app = module.ConfiguredApplication(validate(settings, tmp_path))
    downstream = Mock()
    with pytest.raises(module.RequestError):
        app.accept(input_value, downstream)
    downstream.assert_not_called()
    assert not (tmp_path / "snapshots").exists()
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize("phase", ["review_2", "ANY-value", "0", "Mixed_CASE"])
def test_phase_never_selects_argv(settings, tmp_path, phase):
    module = configuration_module()
    config = validate(settings, tmp_path)
    downstream = Mock(return_value="accepted")
    assert (
        module.ConfiguredApplication(config).accept({"phase": phase}, downstream)
        == "accepted"
    )
    downstream.assert_called_once_with(config, phase)
    assert config.command_argv == tuple(settings["command_argv"])


def test_optional_phase_recorded_even_if_unused(settings, tmp_path):
    settings["output_dir"] = "results/static"
    module = configuration_module()
    app = module.ConfiguredApplication(validate(settings, tmp_path))
    downstream = Mock()
    app.accept({}, downstream)
    app.accept({"phase": "unused"}, downstream)
    assert [call.args[1] for call in downstream.call_args_list] == [None, "unused"]


def test_lifetime_restart_and_immutable_identity(settings, tmp_path, monkeypatch):
    module = configuration_module()
    path = save(tmp_path / "operator.toml", settings)
    original_loader = module.load_configuration
    loader = Mock(wraps=original_loader)
    monkeypatch.setattr(module, "load_configuration", loader)
    app = module.ConfiguredApplication.from_path(path)
    original = app.config
    with pytest.raises(dataclasses.FrozenInstanceError):
        original.user = "changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        app.config = original
    settings["command_argv"] = ["new-tool"]
    save(path, settings)
    downstream = Mock()
    for _ in range(2):
        app.accept({"phase": "repeat"}, downstream)
    assert loader.call_count == 1
    assert all(call.args[0] is original for call in downstream.call_args_list)
    restarted = module.ConfiguredApplication.from_path(path)
    assert restarted.config.command_argv == ("new-tool",)
    assert restarted.config.identity != original.identity
    settings["command_argv"] = []
    save(path, settings)
    app.accept({"phase": "still-original"}, downstream)
    with pytest.raises(module.ConfigurationError, match="command_argv"):
        module.ConfiguredApplication.from_path(path)
    assert app.config is original


def test_startup_loads_once_before_service(settings, tmp_path, monkeypatch):
    from footman import startup

    module = configuration_module()
    path = save(tmp_path / "operator.toml", settings)
    observed = []

    def serve(app):
        settings["command_argv"] = []
        save(path, settings)
        app.accept({"phase": "later"}, lambda config, phase: observed.append(config))
        return 0

    monkeypatch.setattr(startup, "serve", serve, raising=False)
    assert startup.main(["--config", str(path)]) == 0
    assert observed[0].command_argv == ("tool", "two words", "; $literal", "{phase}")
    with pytest.raises(module.ConfigurationError):
        module.ConfiguredApplication.from_path(path)


def test_invalid_existing_toml_refuses_startup(settings, tmp_path):
    settings["command_argv"] = []
    path = save(tmp_path / "invalid.toml", settings)
    result = run(
        [Path(sys.executable).with_name("footman"), "--config", path],
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == b""
    assert b"command_argv" in result.stderr
    assert b"configuration" in result.stderr.lower()
    assert b"not implemented" not in result.stderr.lower()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["invalid.toml", "repository"]
    assert list((tmp_path / "repository").iterdir()) == []


def test_no_repository_configuration_discovery(settings, tmp_path):
    module = configuration_module()
    save(tmp_path / "repository/footman.toml", {"version": 999})
    path = save(tmp_path / "operator.toml", settings)
    assert module.ConfiguredApplication.from_path(path).config.version == 1


@pytest.mark.parametrize("data", [b"not valid TOML = [", b"\xff"])
def test_parse_diagnostics(tmp_path, data):
    module = configuration_module()
    path = tmp_path / "broken.toml"
    write_bytes(path, data)
    with pytest.raises(module.ConfigurationError, match="configuration"):
        module.load_configuration(path)


@pytest.mark.parametrize(
    "template",
    [
        "results/{phase}/../../repository",
        "results/{utc_datetime}/../../escape",
    ],
)
def test_dynamic_output_cannot_escape_storage(settings, tmp_path, template):
    settings["output_dir"] = template
    with pytest.raises(configuration_module().ConfigurationError, match="output_dir"):
        validate(settings, tmp_path)


@pytest.mark.parametrize("field", ["snapshot_directory", "output_dir"])
def test_storage_ancestor_must_be_directory(settings, tmp_path, field):
    write_bytes(tmp_path / "not-directory", b"unchanged")
    settings[field] = "not-directory/child"
    with pytest.raises(configuration_module().ConfigurationError, match=field):
        validate(settings, tmp_path)


def test_nonregular_config_refused_without_blocking(tmp_path):
    module = configuration_module()
    path = tmp_path / "fifo.toml"
    os.mkfifo(path)
    with pytest.raises(module.ConfigurationError, match="regular file"):
        module.load_configuration(path)


def test_config_close_interruption(settings, tmp_path, monkeypatch):
    module = configuration_module()
    settings["command_argv"] = []
    path = save(tmp_path / "operator.toml", settings)
    original = module.PosixDescriptorOps.close
    calls = []
    interruption = KeyboardInterrupt("close interrupted")

    def close(ops, fd):
        calls.append(fd)
        original(ops, fd)
        raise interruption

    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    with pytest.raises(KeyboardInterrupt) as caught:
        module.load_configuration(path)
    assert caught.value is interruption
    assert len(calls) == 1


def test_concurrent_loads_are_independent(settings, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    module = configuration_module()
    path = save(tmp_path / "operator.toml", settings)
    with ThreadPoolExecutor(max_workers=4) as pool:
        configs = list(pool.map(module.load_configuration, [path] * 8))
    assert len({config.identity for config in configs}) == 1
    assert len({id(config) for config in configs}) == 8


def test_example_is_loadable(tmp_path):
    module = configuration_module()
    example = Path(__file__).resolve().parents[1] / "examples/footman.toml"
    text = read_bytes(example).decode()
    (tmp_path / "repository").mkdir()
    path = tmp_path / "operator.toml"
    write_bytes(path, text.encode())
    assert module.load_configuration(path).user == "root"


def test_configuration_read_lifecycle(settings, tmp_path, monkeypatch):
    module = configuration_module()
    path = save(tmp_path / "operator.toml", settings)
    real_read = os.read
    real_close = module.PosixDescriptorOps.close
    closes = []
    owners = []
    original_owner = module.OwnedDescriptor

    class RecordingOwner(original_owner):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            owners.append(self)

    def close(ops, fd):
        closes.append(fd)
        real_close(ops, fd)
        raise OSError("injected close failure")

    monkeypatch.setattr(module, "OwnedDescriptor", RecordingOwner)
    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    # Sole-close failure is visible, not a successfully loaded config.
    with pytest.raises(module.ConfigurationError, match="load") as sole:
        module.load_configuration(path)
    assert "injected close" in str(sole.value.__cause__)
    owners[-1].close()
    assert len(closes) == 1

    # Operation failure retains authority over ordinary release failure.
    def read_failure(fd, size):
        raise OSError("injected read failure")

    monkeypatch.setattr(os, "read", read_failure)
    with pytest.raises(module.ConfigurationError) as combined:
        module.load_configuration(path)
    assert "read failure" in str(combined.value.__cause__)
    secondary = getattr(combined.value.__cause__, "_transaction_secondary", [])
    assert len(secondary) == 1
    assert "injected close failure" in str(secondary[0])
    owners[-1].close()
    assert len(closes) == 2

    interrupt = KeyboardInterrupt("interrupted config read")

    def interrupted(fd, size):
        raise interrupt

    monkeypatch.setattr(os, "read", interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        module.load_configuration(path)
    assert caught.value is interrupt
    secondary = getattr(interrupt, "_transaction_secondary", [])
    assert len(secondary) == 1
    assert "injected close failure" in str(secondary[0])
    owners[-1].close()
    assert len(closes) == 3
    monkeypatch.setattr(os, "read", real_read)


@pytest.mark.parametrize(
    "name", ["{phase}", "{utc_datetime}", "{unknown}", "unmatched{"]
)
@pytest.mark.parametrize(
    "template", ["results/static", "results/{utc_datetime}", "results/{phase}"]
)
def test_configuration_directory_braces_are_literal(settings, tmp_path, name, template):
    module = configuration_module()
    directory = tmp_path / name
    directory.mkdir()
    settings.update(
        repository=str(tmp_path / "repository"),
        snapshot_directory=str(tmp_path / "snapshots"),
        output_dir=template,
    )
    config = module.load_configuration(save(directory / "operator.toml", settings))
    assert config.requires_phase == ("{phase}" in template)
    assert config.output_dir == str(directory / template)
    expected_suffix = () if template == "results/static" else (template.split("/")[-1],)
    expected_root = directory / ("results/static" if not expected_suffix else "results")
    assert config.output_root == expected_root
    assert config.output_suffix == expected_suffix
    downstream = Mock()
    if config.requires_phase:
        with pytest.raises(module.RequestError):
            module.ConfiguredApplication(config).accept({}, downstream)
        downstream.assert_not_called()
    else:
        module.ConfiguredApplication(config).accept({}, downstream)
        downstream.assert_called_once_with(config, None)


@pytest.mark.parametrize("location", ["configuration-directory", "output-directory"])
@pytest.mark.parametrize("name", ["{phase}", "{utc_datetime}", "{unknown}"])
@pytest.mark.parametrize(
    "template", ["results/static", "results/{utc_datetime}", "results/{phase}"]
)
def test_canonical_symlink_target_braces_are_literal(
    settings, tmp_path, location, name, template
):
    module = configuration_module()
    target = tmp_path / name
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    settings.update(
        repository=str(tmp_path / "repository"),
        snapshot_directory=str(tmp_path / "snapshots"),
        output_dir=template,
    )
    if location == "configuration-directory":
        path = alias / "operator.toml"
    else:
        path = tmp_path / "operator.toml"
        settings["output_dir"] = "alias/" + template
    config = module.load_configuration(save(path, settings))
    assert config.output_dir == str(target / template)
    assert config.requires_phase == ("{phase}" in template)
    suffix = () if template == "results/static" else (template.split("/")[-1],)
    assert config.output_suffix == suffix
    assert config.output_root == target / (
        "results/static" if not suffix else "results"
    )


@pytest.mark.parametrize("field", ["snapshot_directory", "output_dir"])
def test_brace_symlink_target_still_rejects_real_overlap(settings, tmp_path, field):
    target = tmp_path / "repository/{phase}"
    target.mkdir()
    (tmp_path / "alias").symlink_to(target, target_is_directory=True)
    settings[field] = "alias/new"
    with pytest.raises(configuration_module().ConfigurationError, match="overlap"):
        validate(settings, tmp_path)


def test_identity_distinguishes_literal_and_template_braces(settings, tmp_path):
    module = configuration_module()
    base = tmp_path / "operator"
    literal_base = base / "{phase}"
    literal_base.mkdir(parents=True)
    settings.update(
        repository=str(tmp_path / "repository"),
        snapshot_directory=str(tmp_path / "snapshots"),
        output_dir="results/static",
    )
    literal = module.validate_configuration(settings, literal_base / "operator.toml")
    settings["output_dir"] = "{phase}/results/static"
    dynamic = module.validate_configuration(settings, base / "operator.toml")
    assert literal.output_dir == dynamic.output_dir
    assert not literal.requires_phase
    assert dynamic.requires_phase
    assert literal.identity != dynamic.identity


def test_validation_plus_close_failure(settings, tmp_path, monkeypatch):
    module = configuration_module()
    settings["command_argv"] = []
    path = save(tmp_path / "operator.toml", settings)
    original = module.PosixDescriptorOps.close
    calls = []

    def close(ops, fd):
        calls.append(fd)
        original(ops, fd)
        raise OSError("secondary close")

    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    with pytest.raises(module.ConfigurationError, match="command_argv") as caught:
        module.load_configuration(path)
    secondary = getattr(caught.value, "_transaction_secondary", [])
    assert len(secondary) == 1
    assert "secondary close" in str(secondary[0])
    assert len(calls) == 1
