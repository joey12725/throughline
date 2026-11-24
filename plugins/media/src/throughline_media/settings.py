"""Small helpers plugins share for switches, flags and setting aliases."""

import argparse


def truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def parse_flags(argv, flags):
    """Map a plugin's own flags onto environment settings.

    `flags` is {flag: (VARIABLE, type)}; a None type is a switch that sets
    "1". The SDK runner owns the remaining arguments (command, --dry-run,
    --once), which are returned unchanged.
    """
    parser = argparse.ArgumentParser(add_help=False)
    for flag, (_, kind) in flags.items():
        if kind is None:
            parser.add_argument(flag, action="store_true")
        else:
            parser.add_argument(flag, type=kind)
    known, rest = parser.parse_known_args(argv)
    env = {}
    for flag, (variable, kind) in flags.items():
        value = getattr(known, flag.lstrip("-").replace("-", "_"))
        if kind is None and value:
            env[variable] = "1"
        elif kind is not None and value is not None:
            env[variable] = str(value)
    return env, rest


def apply_aliases(values, aliases):
    """Fill empty `values[name]` from `values[alias]` for each (name, alias)."""
    for name, alias in aliases:
        if not values.get(name) and values.get(alias):
            values[name] = values[alias]
    return values
