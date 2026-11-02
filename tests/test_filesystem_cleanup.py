"""Upstream Jigbox Phase 1 foundation tests for :mod:`footman.filesystem.cleanup`.

These tests pin the generic, domain-neutral cleanup-precedence accumulator and
its diagnostic-retention helpers, and prove the foundation is import-isolated
from process and application policy.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
import unittest
from pathlib import Path

from footman.filesystem.cleanup import (
    _MAX_SECONDARY_NOTES,
    CleanupFailures,
    attach_secondary,
    carry_secondary_diagnostics,
)

from tests.foundation_imports import (
    FORBIDDEN_DOMAIN_MODULES,
    FOUNDATION_MODULES,
    assert_foundation_imports,
)

_REPO = Path(__file__).resolve().parents[1]
_PACKAGE = _REPO / "src" / "footman" / "filesystem"

_FORBIDDEN_IMPORT_PREFIXES = tuple(name + "." for name in FORBIDDEN_DOMAIN_MODULES)
_FORBIDDEN_MODULES = frozenset(FORBIDDEN_DOMAIN_MODULES)
_AGGREGATE_NAMES = (
    "CleanupFailures",
    "attach_secondary",
    "carry_secondary_diagnostics",
    "DescriptorOps",
    "PosixDescriptorOps",
    "DescriptorError",
    "UnsafeDescriptorError",
    "OwnedDescriptor",
    "DirectoryDescriptor",
)

# An isolated interpreter imports the cleanup module and reports any forbidden
# module it loaded, any aggregate name the package exposed, and whether the
# package declares an ``__all__``.
_ISOLATED_PROBE = """
import json
import sys

import footman.filesystem
import footman.filesystem.cleanup  # noqa: F401  (import side effect is the probe)

forbidden_prefixes = {prefixes!r}
forbidden_modules = {modules!r}
aggregate_names = {aggregate!r}

loaded = sorted(
    name
    for name in sys.modules
    if name in forbidden_modules or name.startswith(forbidden_prefixes)
)
exposed = sorted(name for name in aggregate_names if hasattr(footman.filesystem, name))
print(json.dumps({{"loaded": loaded, "exposed": exposed, "all": hasattr(footman.filesystem, "__all__")}}))
""".format(
    prefixes=_FORBIDDEN_IMPORT_PREFIXES,
    modules=_FORBIDDEN_MODULES,
    aggregate=_AGGREGATE_NAMES,
)


class _Cancellation(BaseException):
    """A cancellation-style, non-I/O process-control exception."""


class _FrozenDomainError(Exception):
    """A domain exception whose value fields are frozen.

    Only exception-machinery attributes (including ``__notes__``) are writable,
    as with a frozen application-specific exception. The
    generic diagnostic helpers must therefore fall back to bounded notes.
    """

    _INTERNAL = frozenset(
        {
            "__notes__",
            "__traceback__",
            "__cause__",
            "__context__",
            "__suppress_context__",
        }
    )

    def __setattr__(self, name: str, value: object) -> None:
        if name in self._INTERNAL:
            object.__setattr__(self, name, value)
            return
        raise AttributeError(f"cannot assign to field {name!r}")


def _raiser(exc: BaseException):
    def action() -> None:
        raise exc

    return action


def _secondary_of(exc: BaseException) -> list[BaseException]:
    return getattr(exc, "_transaction_secondary", [])




class _DiagnosticError(Exception):
    """Local stand-in for a domain exception exposing add_secondary."""

    def __init__(self):
        super().__init__("wrapper")
        self.secondary = []

    def add_secondary(self, exc):
        if exc is not self and not any(item is exc for item in self.secondary):
            self.secondary.append(exc)


def _transaction_wrapper(*secondaries):
    wrapper = _DiagnosticError()
    for exc in secondaries:
        wrapper.add_secondary(exc)
    return wrapper


class CleanupFailuresTruthTableTests(unittest.TestCase):
    def test_no_primary_and_no_failure_returns_none_and_runs_action(self) -> None:
        calls: list[str] = []
        failures = CleanupFailures(None)
        failures.run(lambda: calls.append("only"), ordinary=(OSError,))
        self.assertIsNone(failures.complete())
        self.assertEqual(calls, ["only"])

    def test_ordinary_primary_stays_authoritative(self) -> None:
        primary = ValueError("primary")
        failures = CleanupFailures(primary)
        first = OSError("first cleanup")
        second = OSError("second cleanup")
        failures.run(_raiser(first), ordinary=(OSError,))
        failures.run(lambda: None, ordinary=(OSError,))
        failures.run(_raiser(second), ordinary=(OSError,))
        self.assertIsNone(failures.complete())
        self.assertEqual(_secondary_of(primary), [first, second])

    def test_process_control_primary_precedes_later_interruptions(self) -> None:
        primary = _Cancellation("original interruption")
        failures = CleanupFailures(primary)
        later = _Cancellation("later interruption")
        defect = RuntimeError("unexpected defect")
        ordinary = OSError("ordinary cleanup")
        failures.run(_raiser(ordinary), ordinary=(OSError,))
        failures.run(_raiser(defect), ordinary=(OSError,))
        failures.run(_raiser(later), ordinary=(OSError,))
        with self.assertRaises(_Cancellation) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, primary)
        self.assertEqual(_secondary_of(primary), [later, defect, ordinary])

    def test_single_ordinary_cleanup_failure_without_primary_is_returned(self) -> None:
        failures = CleanupFailures(None)
        ordinary = OSError("only cleanup")
        failures.run(_raiser(ordinary), ordinary=(OSError,))
        self.assertIs(failures.complete(), ordinary)

    def test_multiple_ordinary_failures_attach_remaining(self) -> None:
        failures = CleanupFailures(None)
        first = OSError("one")
        second = OSError("two")
        third = OSError("three")
        failures.run(_raiser(first), ordinary=(OSError,))
        failures.run(_raiser(second), ordinary=(OSError,))
        failures.run(_raiser(third), ordinary=(OSError,))
        self.assertIs(failures.complete(), first)
        self.assertEqual(_secondary_of(first), [second, third])

    def test_unexpected_cleanup_defect_is_raised_unchanged(self) -> None:
        failures = CleanupFailures(None)
        defect = RuntimeError("programmer defect")
        failures.run(_raiser(defect), ordinary=(OSError,))
        with self.assertRaises(RuntimeError) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, defect)

    def test_cleanup_interruption_is_raised_unchanged(self) -> None:
        failures = CleanupFailures(None)
        interruption = KeyboardInterrupt("cleanup interrupted")
        failures.run(_raiser(interruption), ordinary=(OSError,))
        with self.assertRaises(KeyboardInterrupt) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, interruption)

    def test_every_independent_action_runs_exactly_once(self) -> None:
        calls: list[str] = []
        failures = CleanupFailures(ValueError("primary"))

        def record(tag: str, exc: BaseException | None = None):
            def action() -> None:
                calls.append(tag)
                if exc is not None:
                    raise exc

            return action

        failures.run(record("a", OSError("a")), ordinary=(OSError,))
        failures.run(record("b"), ordinary=(OSError,))
        failures.run(record("c", OSError("c")), ordinary=(OSError,))
        failures.run(record("d", OSError("d")), ordinary=(OSError,))
        self.assertIsNone(failures.complete())
        self.assertEqual(calls, ["a", "b", "c", "d"])

    def test_run_after_completion_is_rejected(self) -> None:
        failures = CleanupFailures(None)
        failures.complete()
        with self.assertRaises(RuntimeError):
            failures.run(lambda: None, ordinary=(OSError,))

    def test_second_complete_is_rejected_without_duplicating(self) -> None:
        failures = CleanupFailures(None)
        ordinary = OSError("only")
        failures.run(_raiser(ordinary), ordinary=(OSError,))
        self.assertIs(failures.complete(), ordinary)
        with self.assertRaises(RuntimeError):
            failures.complete()
        self.assertEqual(_secondary_of(ordinary), [])


class AttachSecondaryContractTests(unittest.TestCase):
    def test_duplicate_identity_is_not_attached_twice(self) -> None:
        primary = ValueError("primary")
        duplicate = OSError("cleanup")
        attach_secondary(primary, [duplicate])
        attach_secondary(primary, [duplicate])
        self.assertEqual(_secondary_of(primary), [duplicate])

    def test_distinct_equal_objects_are_both_retained(self) -> None:
        primary = ValueError("primary")
        first = OSError("same")
        second = OSError("same")
        attach_secondary(primary, [first, second])
        self.assertEqual(_secondary_of(primary), [first, second])

    def test_add_secondary_capable_primary_deduplicates_by_identity(self) -> None:
        primary = _transaction_wrapper()
        duplicate = OSError("cleanup")
        attach_secondary(primary, [duplicate, duplicate])
        attach_secondary(primary, [duplicate])
        self.assertEqual(primary.secondary, [duplicate])

    def test_frozen_domain_exception_falls_back_to_bounded_notes(self) -> None:
        primary = _FrozenDomainError("primary")
        first = OSError("first cleanup")
        second = OSError("second cleanup")
        attach_secondary(primary, [first, second])
        self.assertFalse(hasattr(primary, "_transaction_secondary"))
        notes = list(primary.__notes__)
        self.assertEqual(len(notes), 2)
        self.assertIn("secondary cleanup failure", notes[0])
        self.assertIn("first cleanup", notes[0])
        self.assertIn("second cleanup", notes[1])

    def test_note_text_is_bounded(self) -> None:
        primary = _FrozenDomainError("primary")
        attach_secondary(primary, [OSError("x" * 5000)])
        self.assertLessEqual(len(primary.__notes__[0]), 240)

    def test_notes_are_bounded_across_repeated_attachments(self) -> None:
        primary = _FrozenDomainError("primary")
        for index in range(_MAX_SECONDARY_NOTES * 4):
            attach_secondary(primary, [OSError(f"cleanup {index}")])
        generated = [
            note for note in primary.__notes__ if "secondary cleanup failure" in note
        ]
        self.assertEqual(len(generated), _MAX_SECONDARY_NOTES)


class CarrySecondaryDiagnosticsContractTests(unittest.TestCase):

    def test_generic_slot_source_is_carried(self) -> None:
        first = OSError("first")
        second = OSError("second")
        source = ValueError("source")
        source._transaction_secondary = [first, second]  # type: ignore[attr-defined]
        target = ValueError("target")
        carry_secondary_diagnostics(target, source)
        self.assertEqual(_secondary_of(target), [first, second])

    def test_no_diagnostics_is_a_noop(self) -> None:
        source = _transaction_wrapper()
        target = ValueError("target")
        self.assertIsNone(carry_secondary_diagnostics(target, source))
        self.assertFalse(hasattr(target, "_transaction_secondary"))
        self.assertFalse(hasattr(target, "__notes__"))

    def test_target_identity_and_chain_are_preserved(self) -> None:
        source = _transaction_wrapper(OSError("cleanup"))
        target = ValueError("target")
        carry_secondary_diagnostics(target, source)
        self.assertIsInstance(target, ValueError)
        self.assertEqual(str(target), "target")
        self.assertIsNone(target.__cause__)
        self.assertIsNone(target.__context__)

    def test_frozen_target_receives_bounded_notes(self) -> None:
        source = _transaction_wrapper(OSError("cleanup"))
        target = _FrozenDomainError("target")
        carry_secondary_diagnostics(target, source)
        self.assertFalse(hasattr(target, "_transaction_secondary"))
        notes = list(target.__notes__)
        self.assertEqual(len(notes), 1)
        self.assertIn("cleanup", notes[0])
        self.assertLessEqual(len(notes[0]), 240)



class FoundationOwnershipTests(unittest.TestCase):


    def test_shared_callables_are_owned_by_the_foundation(self) -> None:
        self.assertEqual(CleanupFailures.__module__, "footman.filesystem.cleanup")
        self.assertEqual(attach_secondary.__module__, "footman.filesystem.cleanup")
        self.assertEqual(
            carry_secondary_diagnostics.__module__, "footman.filesystem.cleanup"
        )


    def test_public_signatures_are_exact(self) -> None:
        cleanup_init = inspect.signature(CleanupFailures.__init__)
        self.assertEqual(list(cleanup_init.parameters), ["self", "primary"])
        run_params = inspect.signature(CleanupFailures.run).parameters
        self.assertEqual(list(run_params), ["self", "action", "ordinary"])
        self.assertEqual(run_params["ordinary"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(
            list(inspect.signature(carry_secondary_diagnostics).parameters),
            ["target", "source"],
        )


class FoundationImportBoundaryTests(unittest.TestCase):
    def test_cleanup_imports_only_the_standard_library(self) -> None:
        tree = ast.parse((_PACKAGE / "cleanup.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    self.assertIn(root, sys.stdlib_module_names, alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, f"relative import: {node.module}")
                root = (node.module or "").split(".")[0]
                self.assertIn(root, sys.stdlib_module_names, node.module)

    def test_package_initializer_has_no_imports_or_reexports(self) -> None:
        tree = ast.parse((_PACKAGE / "__init__.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            self.assertNotIsInstance(node, (ast.Import, ast.ImportFrom))
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    self.assertFalse(
                        isinstance(target, ast.Name) and target.id == "__all__"
                    )

    def test_no_foundation_module_imports_a_domain_or_transactions(self) -> None:
        for filename in FOUNDATION_MODULES:
            assert_foundation_imports(
                (_PACKAGE / filename).read_text(encoding="utf-8"), filename
            )

    def test_injected_domain_imports_are_rejected_in_every_module(self):
        for filename in FOUNDATION_MODULES:
            source = (_PACKAGE / filename).read_text(encoding="utf-8")
            for module in (*FORBIDDEN_DOMAIN_MODULES, "footman", "footman.filesystem", "third_party"):
                for injection in (f"import {module} as _probe", f"from {module} import probe"):
                    with self.subTest(filename=filename, injection=injection):
                        with self.assertRaisesRegex(AssertionError, "non-foundation import"):
                            assert_foundation_imports(source + "\n" + injection, filename)
            with self.assertRaisesRegex(AssertionError, "non-foundation import"):
                assert_foundation_imports(
                    source + "\nfrom footman.filesystem import cleanup", filename
                )
            with self.assertRaisesRegex(AssertionError, "relative import"):
                assert_foundation_imports(source + "\nfrom .. import startup", filename)


class FoundationImportIsolationTests(unittest.TestCase):
    def test_cleanup_import_does_not_load_other_subsystems(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", _ISOLATED_PROBE],
            cwd=str(_REPO),
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["loaded"], [])
        self.assertEqual(payload["exposed"], [])
        self.assertFalse(payload["all"])


if __name__ == "__main__":
    unittest.main()
