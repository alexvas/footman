"""Phase 5 launch configuration contracts; Docker is not required."""

import dataclasses
import sys

import pytest

from tests.dispatcher_harness import run
from tests.test_configuration import configuration_module, save, settings

__all__ = ["settings"]


IMAGE = "sha256:" + "a" * 64
PREFIX = ["docker", "run", "--detach=true", "--pull=never"]


def candidate(settings):
    return {
        **settings,
        "image_id": IMAGE,
        "container_start_argv": [*PREFIX, "--", "{image_id}", "sleep", "infinity"],
    }


@pytest.mark.parametrize("image", ["d9e853e87e55", "A" * 64, IMAGE])
def test_dev_image_id_forms_configuration(settings, tmp_path, image):
    values = candidate(settings)
    values["image_id"] = image
    config = configuration_module().validate_configuration(values, tmp_path / "op.toml")
    assert config.image_id == image


@pytest.mark.parametrize(
    "image",
    [
        None,
        42,
        "footman:latest",
        "a" * 11,
        "a" * 65,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "g" * 12,
        "a" * 12 + "\n",
    ],
)
def test_invalid_image_id_configuration(settings, tmp_path, image):
    values = candidate(settings)
    values["image_id"] = image
    configuration_module().validate_configuration(
        candidate(settings), tmp_path / "op.toml"
    )
    with pytest.raises(ValueError, match="image_id"):
        configuration_module().validate_configuration(values, tmp_path / "op.toml")
    # TOML has no null: None is a loader-level case, absence a startup case.
    startup_values = {key: value for key, value in values.items() if value is not None}
    assert_startup_rejected(startup_values, tmp_path, "image_id")


@pytest.mark.parametrize(
    "options", [[], ["--privileged=true"], ["--label=a=b", "--hostname="]]
)
def test_launch_grammar_accepts_equals_options(settings, tmp_path, options):
    values = candidate(settings)
    values["container_start_argv"] = [
        *PREFIX,
        *options,
        "--",
        "{image_id}",
        "sleep",
        "--",
        "two words",
    ]
    config = configuration_module().validate_configuration(values, tmp_path / "op.toml")
    assert tuple(values["container_start_argv"]) == config.container_start_argv


@pytest.mark.parametrize(
    "argv",
    [
        ["env", *PREFIX, "--", "{image_id}"],
        ["/usr/bin/docker", *PREFIX[1:], "--", "{image_id}"],
        ["docker", "run", "--detach=true", "--", "{image_id}"],
        [*PREFIX, "--pull=never", "--", "{image_id}"],
        [*PREFIX, "--pull=always", "--", "{image_id}"],
        ["docker", "run", "--detach=true", "--pull=missing", "--", "{image_id}"],
        ["docker", "run", "--detach=true", "--pull", "never", "--", "{image_id}"],
        [
            "docker",
            "--context=other",
            "run",
            "--detach=true",
            "--pull=never",
            "--",
            "{image_id}",
        ],
        [*PREFIX, "--", "{snapshot_path}"],
        [*PREFIX, "--", "{image_id}", "{unknown}"],
        [*PREFIX, "--detach=false", "--", "{image_id}"],
        [*PREFIX, "-v", "source:target", "--", "{image_id}"],
        [*PREFIX, "--privileged", "--", "{image_id}"],
        [*PREFIX, "--name", "value", "--", "{image_id}"],
        [*PREFIX, "--=value", "--", "{image_id}"],
        [*PREFIX, "{image_id}"],
        [*PREFIX, "--", "unrelated", "{image_id}"],
        [*PREFIX, "--name={image_id}", "--", "{image_id}"],
        [*PREFIX, "--", "prefix{image_id}"],
        [*PREFIX, "--", "{image_id}", "{image_id}"],
        [*PREFIX, "--", "{image_id}", "{snapshot_path}"],
    ],
)
def test_launch_grammar_rejects_alternate_shapes(settings, tmp_path, argv):
    values = candidate(settings)
    values["container_start_argv"] = argv
    configuration_module().validate_configuration(
        candidate(settings), tmp_path / "op.toml"
    )
    with pytest.raises(ValueError):
        configuration_module().validate_configuration(values, tmp_path / "op.toml")
    assert_startup_rejected(values, tmp_path, "container_start_argv")


def assert_startup_rejected(values, tmp_path, diagnostic):
    path = save(tmp_path / "invalid.toml", values)
    result = run(
        [sys.executable, "-m", "footman", "--config", path], cwd=tmp_path, check=False
    )
    assert result.returncode != 0
    assert result.stdout == b""
    assert diagnostic.encode() in result.stderr
    assert not (tmp_path / "snapshots").exists()
    assert not (tmp_path / "results").exists()


def test_missing_image_id_rejected(settings, tmp_path):
    values = candidate(settings)
    del values["image_id"]
    assert_startup_rejected(values, tmp_path, "image_id")


@pytest.mark.parametrize("field", ["container_start_argv", "command_argv"])
@pytest.mark.parametrize("bad", ["", "bad\x00arg"])
def test_every_argument_nonempty_nul_free(settings, tmp_path, field, bad):
    baseline = candidate(settings)
    configuration_module().validate_configuration(baseline, tmp_path / "op.toml")
    for index in range(len(baseline[field])):
        values = dict(baseline)
        values[field] = list(baseline[field])
        values[field][index] = bad
        assert_startup_rejected(values, tmp_path, field)


def test_image_id_lifetime_and_effective_identity(settings, tmp_path):
    module = configuration_module()
    path = save(tmp_path / "op.toml", candidate(settings))
    app = module.ConfiguredApplication.from_path(path)
    original = app.config
    changed = candidate(settings) | {"image_id": "c" * 64}
    save(path, changed)
    assert app.accept({"phase": "build"}, lambda cfg, phase: cfg) is original
    restarted = module.ConfiguredApplication.from_path(path)
    assert restarted.config.image_id == "c" * 64
    assert restarted.config.identity != original.identity
    with pytest.raises(dataclasses.FrozenInstanceError):
        original.image_id = "c" * 64
    save(path, changed | {"image_id": "bad"})
    with pytest.raises(ValueError, match="image_id"):
        module.ConfiguredApplication.from_path(path)
