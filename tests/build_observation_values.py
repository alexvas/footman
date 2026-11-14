"""Inert immutable JSON-shaped build binding values, shared without build policy."""


def _immutable(*args, **kwargs):
    raise TypeError("build binding values are immutable")


class _FrozenDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = (
        __ior__
    ) = _immutable


class _FrozenList(list):
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = (
        reverse
    ) = sort = __iadd__ = __imul__ = _immutable


def freeze(value):
    if isinstance(value, dict):
        return _FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze(item) for item in value)
    return value


def check_binding_values(binding_type, root):
    """No acquisition: check defensive copying and nested mutation rejection."""
    identity = {
        "schema_version": 1,
        "roots": ["src/", "pyproject.toml", "README.md"],
        "sha256": "a" * 64,
    }
    operation = {
        "argv": ["uv", "build"],
        "status": 0,
        "pre_identity": identity,
        "post_identity": identity,
    }
    inventory = ({"path": "README.md", "kind": "file", "sha256": "b" * 64},)
    value = binding_type(
        root,
        root / "wheel.whl",
        "a" * 64,
        root / "sdist.tar.gz",
        "b" * 64,
        identity,
        inventory,
        operation,
    )
    operation["status"] = 17
    identity["roots"].append("unowned-root")
    inventory[0]["sha256"] = "c" * 64
    assert value.command["status"] == 0
    assert len(value.build_input_identity["roots"]) == 3
    assert value.inventory[0]["sha256"] == "b" * 64
    for mutate in (
        lambda: value.command.__setitem__("status", 17),
        lambda: value.command["argv"].append("--out-dir"),
        lambda: value.build_input_identity["roots"].clear(),
        lambda: value.inventory[0].update(sha256="0" * 64),
        lambda: value.command["post_identity"].__setitem__("sha256", "0" * 64),
    ):
        try:
            mutate()
        except TypeError:
            pass
        else:
            raise AssertionError("build binding was mutable")
