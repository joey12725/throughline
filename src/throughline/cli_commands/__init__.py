"""Commands of `throughline lib`, one module per component.

A module in this package exports register(group) and adds its commands (or
sub-groups) to the given typer group. throughline.cli mounts that group as
`throughline lib` and calls load() once. Modules may use
`from ..cli import output, request` at the top: the loader runs at the end of
throughline.cli, after both exist.

Nothing here may add to the root command list or to the token, identity or
track groups: their help has a fixed size budget (INT-1).
"""

import importlib
import pkgutil

_loaded = []


def names():
    return sorted(info.name for info in pkgutil.iter_modules(__path__))


def load(group):
    """Call register(group) of every command module, in name order, once."""
    for name in names():
        if name in _loaded:
            continue
        module = importlib.import_module(f"{__name__}.{name}")
        register = getattr(module, "register", None)
        if register is None:
            continue
        _loaded.append(name)
        register(group)
    return list(_loaded)


# Importing a command module directly must still build the whole CLI first;
# throughline.cli ends by calling load(), so the order of imports never matters.
from .. import cli as _cli  # noqa: E402,F401
