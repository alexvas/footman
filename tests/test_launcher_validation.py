"""Startup, inspection and image-selection launcher contracts."""

import pytest

from tests.launcher_case_support import (
    ID,
    IMAGE,
    START,
    api,
    config,
    dispatch,
    source,
    steps,
)
from tests.launcher_support import DockerSequence

__all__ = ["source"]


@pytest.mark.sleepy
@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"a" * 12,
        b"z" * 64,
        (ID + "\n\n").encode(),
        (ID + " extra").encode(),
        (ID + "\r\n").encode(),
        (ID + "\n" + ID).encode(),
    ],
)
def test_invalid_startup_forbids_inspection(tmp_path, source, output):
    module = api()
    script = steps(source)
    script[0]["stdout"] = output
    seq = DockerSequence(tmp_path, script)
    run = module.ContainerExecution(config(), source, launch=seq.launch)
    with pytest.raises(module.ContainerError):
        run.execute(lambda *a: None, lambda *a: pytest.fail("command"))
    assert len(seq.calls()) == 1
    assert run.container_id is None
    seq.assert_settled()


@pytest.mark.sleepy
@pytest.mark.parametrize(
    "record",
    [
        b"",
        b"bad|running",
        ("c" * 64 + "|running").encode(),
        (ID + "|exited").encode(),
        (ID + "|Running").encode(),
        (ID + "|running\n\n").encode(),
        (ID + "|running\r\n").encode(),
        (ID + "\n|running").encode(),
        ID.encode(),
    ],
)
def test_invalid_inspection_forbids_copy(tmp_path, source, record):
    module = api()
    script = steps(source)
    script[1]["stdout"] = record
    seq = DockerSequence(tmp_path, script)
    run = module.ContainerExecution(config(), source, launch=seq.launch)
    with pytest.raises(module.ContainerError):
        run.execute(lambda *a: None, lambda *a: pytest.fail("command"))
    assert len(seq.calls()) == 2
    assert run.container_id == ID
    assert run.infrastructure_operations[0].diagnostics
    seq.assert_settled()


@pytest.mark.sleepy
@pytest.mark.parametrize("metadata", ["", "|", "|bad", "|sha256:123", "|" + IMAGE])
@pytest.mark.parametrize("newline", ["", "\n"])
def test_inspection_unknown_image_is_diagnostic_only(
    tmp_path, source, metadata, newline
):
    script = steps(source)
    script[1]["stdout"] = (ID.upper() + "|running" + metadata + newline).encode()
    run, seq, _, _ = dispatch(tmp_path, source, script)
    assert len(seq.calls()) == 5
    assert run.observed_image_id == (IMAGE if metadata == "|" + IMAGE else None)
    assert bool(run.infrastructure_operations[0].diagnostics) == (
        metadata != "|" + IMAGE
    )


@pytest.mark.sleepy
@pytest.mark.parametrize("image", ["d9e853e87e55", IMAGE])
@pytest.mark.parametrize("resolution", ["error", "hex-name-collision", "retagged"])
def test_dev_image_id_forms(tmp_path, source, image, resolution):
    module = api()
    cfg = config(image_id=image)
    script = steps(source, cfg)
    script[0]["argv"] = [*START[:5], image, *START[6:]]
    if resolution == "error":
        script[0].update(
            status=125,
            stderr=b"No such local image; hexadecimal operand interpreted as name",
        )
    seq = DockerSequence(tmp_path, script)
    run = module.ContainerExecution(cfg, source, launch=seq.launch)
    if resolution == "error":
        with pytest.raises(module.ContainerError):
            run.execute(lambda *a: None, lambda *a: pytest.fail("command"))
        assert len(seq.calls()) == 1
    else:
        run.execute(lambda *a: None, lambda *a: None)
        assert run.observed_image_id == IMAGE
        assert len(seq.calls()) == 5
    assert seq.parameters[0][0] == script[0]["argv"]
    assert seq.parameters[0][0].count("--pull=never") == 1
    seq.assert_settled()
