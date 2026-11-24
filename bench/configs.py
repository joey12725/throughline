"""Named system configurations a benchmark run is made under.

A scoreboard row is only comparable with another made under the same
settings, so every v1.1 suite records the configuration by name with its
settings and the checked-out commit. The harness never changes a running
system: a configuration names the environment the system was started with
(tests apply it to the in-process app; a deployment is started with it).
"""

import os
from pathlib import Path

from . import scoreboard

SYNONYMS = str(Path(__file__).with_name("synonyms.json"))

CONFIGS = {
    "offline-local": dict(
        description=(
            "Deterministic local providers, no credentials: the local embedder"
            " with the bench synonym file, the local reranker, semantic model"
            " and fixed-plan controller."
        ),
        env=dict(
            THROUGHLINE_EMBEDDING_PROVIDER="local",
            THROUGHLINE_EMBEDDING_DIMENSIONS="256",
            THROUGHLINE_LOCAL_SYNONYMS=SYNONYMS,
            THROUGHLINE_RERANK_PROVIDER="local",
            THROUGHLINE_SEMANTIC_PROVIDER="local",
            THROUGHLINE_CONTROLLER_PROVIDER="local",
        ),
    ),
    "keyword-only": dict(
        description="No embedding provider: meaning legs are unavailable.",
        env=dict(
            THROUGHLINE_EMBEDDING_PROVIDER="none",
            THROUGHLINE_RERANK_PROVIDER="local",
            THROUGHLINE_SEMANTIC_PROVIDER="local",
            THROUGHLINE_CONTROLLER_PROVIDER="local",
        ),
    ),
    "deployment": dict(
        description="Whatever the target system was started with (not set here).",
        env={},
    ),
}
DEFAULT = "offline-local"


def environment(name=DEFAULT):
    """The environment variables a configuration sets."""
    try:
        return dict(CONFIGS[name]["env"])
    except KeyError:
        raise KeyError(f"unknown configuration {name}") from None


def record(name=DEFAULT, **overrides):
    """What a scoreboard row records about its configuration: name,
    description, settings (with overrides, for example a fault) and commit.
    For 'deployment' the settings are the THROUGHLINE_*_PROVIDER variables
    visible to the harness, which may differ from the target's."""
    if name not in CONFIGS:
        raise KeyError(f"unknown configuration {name}")
    settings = environment(name)
    if name == "deployment":
        settings = {
            key: value
            for key, value in sorted(os.environ.items())
            if key.startswith("THROUGHLINE_") and key.endswith("_PROVIDER")
        }
    settings.update(overrides)
    return dict(
        name=name,
        description=CONFIGS[name]["description"],
        settings=settings,
        commit=scoreboard.commit(),
    )
