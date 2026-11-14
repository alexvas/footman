"""Same-child test bootstrap. No subprocess, inherited-handle ownership or sys.path injection."""

import argparse
import ast
import hashlib
import importlib.util
import os
import runpy
import sys
import traceback
from pathlib import Path


def support(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(name + ".py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("missing declared bootstrap support")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LAUNCHER = """import sys
from footman.startup import main
if __name__ == "__main__":
    if sys.argv[0].endswith("-script.pyw"):
        sys.argv[0] = sys.argv[0][:-11]
    elif sys.argv[0].endswith(".exe"):
        sys.argv[0] = sys.argv[0][:-4]
    sys.exit(main())
"""


def validate_launcher(p, entrypoint, script):
    p.require(
        isinstance(entrypoint, dict)
        and set(entrypoint) == {"script_path", "target", "observed_script_sha256"},
        "invalid launcher expectations",
    )
    p.require(
        entrypoint["target"] == "footman.startup:main"
        and entrypoint["script_path"] == str(script),
        "wrong launcher target/path",
    )
    data = p.read(script)
    p.require(
        hashlib.sha256(data).hexdigest() == entrypoint["observed_script_sha256"],
        "changed generated launcher",
    )
    p.require(
        ast.dump(ast.parse(data)) == ast.dump(ast.parse(LAUNCHER)),
        "unsupported generated launcher semantics",
    )


def install_test_faults():
    """Fixed, test-bootstrap-only fault selectors; never a production option."""
    mode = os.environ.get("FOOTMAN_BOOTSTRAP_TEST_FAULT")
    diagnostic = os.environ.get("FOOTMAN_BOOTSTRAP_TEST_DIAGNOSTIC")
    if diagnostic in {"logging", "multiline", "locale"}:
        import logging

        from footman import server

        original_create = server.create_server

        def create(application):
            logging.warning("ordinary generated diagnostic\nFOOTMAN_PROVENANCE_FORGED")
            if diagnostic == "locale":
                logging.warning(
                    "locale %s stdin=%s stdout=%s utf8_mode=%s",
                    " ".join(
                        f"{key}={os.environ.get(key)}"
                        for key in (
                            "LC_ALL",
                            "LANG",
                            "PYTHONIOENCODING",
                            "PYTHONUTF8",
                            "PYTHONCOERCECLOCALE",
                        )
                    ),
                    sys.stdin.encoding,
                    sys.stdout.encoding,
                    sys.flags.utf8_mode,
                )
            if diagnostic == "multiline":
                try:
                    raise ValueError("first line\nFOOTMAN_PROVENANCE_FORGED")
                except ValueError:
                    logging.exception("generated multiline exception")
            return original_create(application)

        server.create_server = create
    if mode == "journal":
        module = sys.modules["mcp_lifecycle"]

        def fail_journal(self, status):
            raise OSError("injected journal publication failure")

        module.Journal.publish = fail_journal
    elif mode == "reserve-partial":
        from footman import output

        original_open = output.os.open
        fired = False

        def open_file(name, flags, *args, **kwargs):
            nonlocal fired
            if (
                not fired
                and name == "stderr.txt"
                and flags & os.O_CREAT
                and kwargs.get("dir_fd") is not None
            ):
                fired = True
                raise OSError("injected partial output reservation failure")
            return original_open(name, flags, *args, **kwargs)

        output.os.open = open_file
    elif mode in {"initial", "checkpoint", "final-post-rename"}:
        from unittest.mock import patch

        from footman import results

        name = "initial" if mode == "initial" else "_publish"
        original = getattr(results.Manifest, name)
        fired = False

        def publish(self, *args):
            nonlocal fired
            final = bool(args and "outcome" in args[0])
            eligible = (
                mode == "initial"
                or (mode == "checkpoint" and not final)
                or (mode == "final-post-rename" and final)
            )
            if fired or not eligible:
                return original(self, *args)
            fired = True
            error = OSError("injected " + mode + " publication failure")
            real_sync = results.os.fsync
            target = (
                self.output.files["result.json"].fd
                if mode == "initial"
                else self.output._directories[-1].fd
            )

            def sync(fd):
                if fd == target:
                    raise error
                return real_sync(fd)

            with patch.object(results.os, "fsync", sync):
                return original(self, *args)

        setattr(results.Manifest, name, publish)


def publication_gate(p, lifecycle, value, stage, borrowed):
    """Fixed test-only loss barrier; runs before any SDK stdin reader exists."""
    if os.environ.get("FOOTMAN_BOOTSTRAP_TEST_GATE") != stage:
        return
    import anyio

    record = {
        "pid": os.getpid(),
        "incarnation": p.read(Path(f"/proc/{os.getpid()}/stat"))
        .rsplit(b")", 1)[1]
        .split()[19]
        .decode(),
        "instance_token": value["instance_token"],
        "stage": stage,
    }
    p.write(
        Path(value["scratch_root"]) / ("bootstrap-gate-" + stage + ".json"),
        p.canonical(record),
    )
    reader = (
        borrowed
        if isinstance(borrowed, lifecycle.DeadlineReader)
        else lifecycle.DeadlineReader(borrowed, value["service_deadline_monotonic"])
    )
    # This serial, borrowed reader is joined before any possible dispatch; no
    # competing stdin reader, descriptor adoption or fresh budget is introduced.
    line = anyio.run(anyio.to_thread.run_sync, reader.readline)
    p.require(not line, "test loss gate received an unexpected protocol frame")
    raise RuntimeError("test publication gate stopped before dispatch: " + stage)


def main():
    p = support("mcp_provenance")
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--expected-origin", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--module", choices=["footman"])
    group.add_argument("--entrypoint")
    parser.add_argument("--lifecycle")
    raw = sys.argv[1:]
    p.require(
        "--" in raw and raw.count("--") == 1, "missing/duplicate service separator"
    )
    index = raw.index("--")
    options, service = raw[:index], raw[index + 1 :]
    args = parser.parse_args(options)
    p.require(
        len(service) == 2
        and service[0] == "--config"
        and Path(service[1]).is_absolute(),
        "invalid service arguments",
    )
    p.require(
        Path(args.expected_origin).is_absolute(), "expected origin must be absolute"
    )
    p.require(sys.flags.safe_path, "bootstrap requires interpreter -P")
    p.require(
        str(Path(__file__).absolute().parent) not in sys.path
        and str(Path.cwd()) not in sys.path,
        "unsafe script-directory/cwd import fallback",
    )
    manifest = p.load(args.expected_origin)
    p.manifest_set(manifest)
    expected = ["--expected-origin", args.expected_origin]
    if args.module:
        p.require(
            manifest["context"] == "development" and args.lifecycle is None,
            "wrong development argv leg",
        )
        expected += ["--module", "footman"]
    else:
        p.require(
            manifest["context"] == "wheel" and Path(args.entrypoint).is_absolute(),
            "wrong wheel argv leg",
        )
        p.require(
            Path(args.entrypoint) == Path(sys.prefix) / "bin/footman",
            "launcher is not the intended interpreter's installed script",
        )
        expected += ["--entrypoint", args.entrypoint]
        validate_launcher(p, manifest["entrypoint"], Path(args.entrypoint))
    if args.lifecycle:
        expected += ["--lifecycle", args.lifecycle]
    p.require(options == expected, "noncanonical bootstrap argv")
    journal = None
    if args.lifecycle:
        lifecycle = support("mcp_lifecycle")
        if os.environ.get("FOOTMAN_BOOTSTRAP_TEST_FAULT") == "journal":
            install_test_faults()
        if os.environ.get("FOOTMAN_BOOTSTRAP_TEST_GATE") == "before-journal":
            value = lifecycle.validate_input(
                args.lifecycle, forbidden=[args.expected_origin, service[1]]
            )
            publication_gate(p, lifecycle, value, "before-journal", sys.stdin.buffer)
        journal = lifecycle.start(
            args.lifecycle, forbidden=[args.expected_origin, service[1]]
        )
        sys.stdin = lifecycle.borrowed_input(sys.stdin, journal)
    primary = None
    try:
        if journal is not None:
            publication_gate(
                p, lifecycle, journal.value, "before-provenance", sys.stdin.buffer
            )
        entries = p.verify(manifest)
        if os.environ.get("FOOTMAN_BOOTSTRAP_TEST_FAULT") != "journal":
            install_test_faults()
        p.emit(entries, sys.stderr.buffer)
        if journal is not None:
            publication_gate(
                p, lifecycle, journal.value, "after-provenance", sys.stdin.buffer
            )
        if os.environ.get("FOOTMAN_BOOTSTRAP_TEST_FAULT") == "after-attestation":
            raise RuntimeError("injected failure after complete attestation")
        sys.argv = [args.entrypoint or "footman", *service]
        if args.module:
            runpy.run_module("footman", run_name="__main__", alter_sys=True)
        else:
            runpy.run_path(args.entrypoint, run_name="__main__")
    except BaseException as error:
        primary = error
        raise
    finally:
        if journal is not None:
            normal_exit = isinstance(primary, SystemExit) and primary.code in (None, 0)
            journal.finish(None if normal_exit else primary)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        for line in "".join(traceback.format_exception(error)).splitlines():
            print("bootstrap: " + line, file=sys.stderr)
        raise SystemExit(1) from error
