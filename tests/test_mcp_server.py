"""Real SDK/stdio service contracts, distinct from the protocol peer."""

from pathlib import Path

import pytest

from tests.test_mcp_transport import support

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("context", ["development", "wheel"])
def test_real_server_initialization_and_exact_phase_only_discovery(tmp_path, context):
    support("mcp_contract_support").exercise_discovery(ROOT, tmp_path, context)


@pytest.mark.parametrize(
    "arguments",
    [
        {"phase": ""},
        {"phase": ".."},
        {"phase": "a/b"},
        {"phase": 1},
        {"command": ["evil"]},
        {"config": "other"},
        {},
    ],
)
def test_real_server_invalid_input_has_no_run_side_effects(tmp_path, arguments):
    support("mcp_contract_support").exercise_invalid_request(ROOT, tmp_path, arguments)


@pytest.mark.parametrize("status", [0, 7, 127])
def test_dev_exec_status_and_streams_real_mcp(tmp_path, status):
    support("mcp_contract_support").exercise_response(ROOT, tmp_path, "command", status)


@pytest.mark.parametrize("failure", ["launch", "inspect", "mkdir", "copy"])
def test_real_server_precommand_failure_has_null_status_and_created_paths(
    tmp_path, failure
):
    support("mcp_contract_support").exercise_response(ROOT, tmp_path, failure, 9)


@pytest.mark.parametrize("stage", ["initial", "checkpoint", "final-post-rename"])
def test_publication_error_real_mcp_and_next_request_same_server(tmp_path, stage):
    support("mcp_contract_support").exercise_publication(ROOT, tmp_path, stage)


@pytest.mark.parametrize(
    "scenario",
    ["disconnect", "concurrent", "partial-frame", "eof-timeout", "envelope-expired"],
)
def test_real_client_disconnect_supervision_without_retry(tmp_path, scenario):
    support("mcp_contract_support").exercise_supervision(ROOT, tmp_path, scenario)


@pytest.mark.parametrize(
    "scenario",
    ["logging", "multiline", "startup", "configuration", "helper-failure", "direct"],
)
def test_real_server_generated_stderr_namespace_and_protocol_purity(tmp_path, scenario):
    support("mcp_contract_support").exercise_diagnostics(ROOT, tmp_path, scenario)


@pytest.mark.parametrize("option", ["--lifecycle", "--unknown", "run", "mcp"])
def test_production_rejects_test_bootstrap_and_dispatch_options(tmp_path, option):
    support("mcp_contract_support").exercise_production_rejection(
        ROOT, tmp_path, option
    )


def test_startup_configuration_lifetime_across_real_client_requests(tmp_path):
    support("mcp_contract_support").exercise_configuration_lifetime(ROOT, tmp_path)


@pytest.mark.parametrize("interrupted", [False, True])
def test_run_constructor_failure_releases_output_once_preserving_authority(
    tmp_path, monkeypatch, interrupted
):
    from footman import server
    from footman.configuration import ConfiguredApplication
    from footman.filesystem.descriptors import OwnedDescriptor

    _, config = support("mcp_contract_support").configuration(tmp_path)
    app = ConfiguredApplication.from_path(config)
    primary = (
        KeyboardInterrupt("constructor interrupted")
        if interrupted
        else OSError("constructor operation failed")
    )
    close_error = OSError("independent output close failed")
    captured, released = [], []
    actual_reserve, actual_close = server.reserve_request, OwnedDescriptor.close

    def reserve(*args):
        owner = actual_reserve(*args)
        captured.append(owner)
        return owner

    def construct(*args):
        raise primary

    def close(self):
        live = not self.released and self.label.startswith("retained output ")
        actual_close(self)
        if live:
            released.append(self)
            if self.label == "retained output stdout.txt":
                raise close_error

    monkeypatch.setattr(server, "reserve_request", reserve)
    monkeypatch.setattr(server, "RetainedRun", construct)
    monkeypatch.setattr(OwnedDescriptor, "close", close)
    if interrupted:
        with pytest.raises(KeyboardInterrupt) as caught:
            server.execute(app, {})
        assert caught.value is primary
    else:
        response = server.execute(app, {})
        assert (
            response["outcome"] == "output_failed" and response["return_code"] is None
        )
        import base64

        assert base64.b64decode(
            response["diagnostics"]["failure"]["message_base64"]
        ).decode() == str(primary)
        assert base64.b64decode(
            response["diagnostics"]["cleanup_failure"]["message_base64"]
        ).decode() == str(close_error)
        assert all(
            Path(response[name]).is_file()
            for name in (
                "stdoutPath",
                "stderrPath",
                "startupStdoutPath",
                "startupStderrPath",
                "resultPath",
            )
        )
    assert len(released) == 5 and len({id(owner) for owner in released}) == 5
    assert all(owner.released for owner in captured[0]._files.values())
    before = len(released)
    captured[0].close()
    assert len(released) == before
    assert not (tmp_path / "snapshots").exists()


@pytest.mark.parametrize("case", ["eof", "not-eof", "setup", "setup-interruption"])
def test_sdk_server_facades_and_eof_error_boundary(tmp_path, monkeypatch, case):
    import io
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import Mock

    import anyio
    import mcp.server.stdio as sdk_stdio

    from footman import server
    from footman.configuration import ConfiguredApplication

    _, config = support("mcp_contract_support").configuration(tmp_path)
    app = ConfiguredApplication.from_path(config)
    marker = (
        KeyboardInterrupt("facade setup")
        if case == "setup-interruption"
        else anyio.ClosedResourceError()
    )
    handles = [io.BytesIO(), io.BytesIO()]

    class FakeServer:
        def create_initialization_options(self):
            return None

        async def run(self, incoming, outgoing, options):
            if case == "eof":
                with pytest.raises(StopAsyncIteration):
                    await anext(incoming)
            raise marker

    @asynccontextmanager
    async def stdio(*, stdin, stdout):
        assert isinstance(stdin, server.BorrowedText) and isinstance(
            stdout, server.BorrowedText
        )
        if case.startswith("setup"):
            raise marker
        yield stdin, stdout

    monkeypatch.setattr(server, "create_server", lambda application: FakeServer())
    monkeypatch.setattr(server, "stdio_server", stdio)
    default_wrapper = Mock(
        side_effect=AssertionError("SDK default wrapper acquired inherited handle")
    )
    monkeypatch.setattr(sdk_stdio, "TextIOWrapper", default_wrapper)
    with monkeypatch.context() as scoped:
        scoped.setattr(server.sys, "stdin", SimpleNamespace(buffer=handles[0]))
        scoped.setattr(server.sys, "stdout", SimpleNamespace(buffer=handles[1]))
        if case == "eof":
            anyio.run(server.run, app)
        elif case == "setup-interruption":
            with pytest.raises(KeyboardInterrupt) as caught:
                anyio.run(server.run, app)
            assert caught.value is marker
        else:
            with pytest.raises(BaseExceptionGroup) as caught:
                anyio.run(server.run, app)
            assert marker in caught.value.exceptions
    default_wrapper.assert_not_called()
    assert all(not handle.closed for handle in handles)
    for handle in handles:
        handle.close()  # Test-owned BytesIO only; never a runtime OS handle.


def test_startup_generated_exception_cannot_collide_with_reserved_namespace(
    tmp_path, monkeypatch, capsys
):
    from footman import startup

    _, config = support("mcp_contract_support").configuration(tmp_path)

    def fail(application):
        raise ValueError("generated first line\nFOOTMAN_PROVENANCE_FORGED")

    monkeypatch.setattr(startup, "serve", fail)
    assert startup.main(["--config", str(config)]) == 1
    observed = capsys.readouterr()
    assert not observed.out
    assert "FOOTMAN_PROVENANCE_FORGED" in observed.err
    assert not any(
        line.startswith("FOOTMAN_PROVENANCE_") for line in observed.err.splitlines()
    )


def test_startup_does_not_translate_control_interruption(tmp_path, monkeypatch):
    from footman import startup

    _, config = support("mcp_contract_support").configuration(tmp_path)
    marker = KeyboardInterrupt("service interrupted")

    def fail(application):
        raise marker

    monkeypatch.setattr(startup, "serve", fail)
    with pytest.raises(KeyboardInterrupt) as caught:
        startup.main(["--config", str(config)])
    assert caught.value is marker


def test_real_sdk_stdio_default_wrappers_are_bypassed(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace
    from unittest.mock import Mock

    import anyio
    import mcp.server.stdio as sdk_stdio

    from footman import server
    from footman.configuration import ConfiguredApplication

    _, config = support("mcp_contract_support").configuration(tmp_path)
    application = ConfiguredApplication.from_path(config)
    borrowed_in, borrowed_out = io.BytesIO(), io.BytesIO()
    forbidden = Mock(
        side_effect=AssertionError("default SDK fd-owning wrapper was constructed")
    )
    monkeypatch.setattr(sdk_stdio, "TextIOWrapper", forbidden)
    with monkeypatch.context() as scoped:
        scoped.setattr(server.sys, "stdin", SimpleNamespace(buffer=borrowed_in))
        scoped.setattr(server.sys, "stdout", SimpleNamespace(buffer=borrowed_out))
        anyio.run(server.run, application)
        anyio.run(server.run, application)
    forbidden.assert_not_called()
    assert (
        not borrowed_in.closed
        and not borrowed_out.closed
        and not borrowed_out.getvalue()
    )
    borrowed_in.close()
    borrowed_out.close()


def test_real_mcp_partial_reservation_reports_only_created_paths(tmp_path):
    support("mcp_contract_support").exercise_partial_reservation(ROOT, tmp_path)


@pytest.mark.parametrize("operation", [None, "ordinary", "interruption"])
@pytest.mark.parametrize("cleanup", ["ordinary", "interruption"])
def test_logging_cleanup_preserves_operation_and_interruption(
    tmp_path, monkeypatch, operation, cleanup
):
    from footman import server
    from footman.configuration import ConfiguredApplication

    _, config = support("mcp_contract_support").configuration(tmp_path)
    app = ConfiguredApplication.from_path(config)
    primary = (
        KeyboardInterrupt("service interruption")
        if operation == "interruption"
        else OSError("service operation")
        if operation
        else None
    )
    release = (
        KeyboardInterrupt("logging interruption")
        if cleanup == "interruption"
        else OSError("logging sole close")
    )
    handlers = []
    previous = server.logging.getLogger().handlers[:]
    stderr = server.sys.stderr

    class Handler:
        def __init__(self, borrowed):
            assert borrowed is stderr
            self.attempts = 0
            handlers.append(self)

        def setFormatter(self, formatter):
            pass

        def close(self):
            self.attempts += 1
            raise release

    def run(function, application):
        if primary is not None:
            raise primary

    expected = (
        primary
        if isinstance(primary, KeyboardInterrupt)
        else release
        if isinstance(release, KeyboardInterrupt)
        else primary or release
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(server.logging, "StreamHandler", Handler)
        scoped.setattr(server.anyio, "run", run)
        with pytest.raises(BaseException) as caught:
            server.serve(app)
    assert caught.value is expected
    assert len(handlers) == 1 and handlers[0].attempts == 1
    assert (
        server.logging.getLogger().handlers == previous and server.sys.stderr is stderr
    )
    if primary is not None and release is not expected:
        assert any(
            error is release
            for error in getattr(expected, "_transaction_secondary", ())
        )
    if primary is not None and primary is not expected:
        assert any(
            error is primary
            for error in getattr(expected, "_transaction_secondary", ())
        )
