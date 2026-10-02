"""The zero-model judgment kernel.

`core/` is the stable side of the project: it decides whether a block is
admissible, whether the pieces agree semantically, and whether the whole
assembly holds together. It is pure stdlib, deterministic, and performs no
I/O -- all environment contact enters through ports implemented elsewhere and
through data injected by the caller.

Dependency rule (enforced by `tests/test_arch_guard.py`):
    every arrow points INTO core, never out of it.
"""

from . import model

__all__ = ["model"]
