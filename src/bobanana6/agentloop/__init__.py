"""Hermes-derived worker runtime (steps 15-21: errors, events, metrics, state,
attribution, concurrency, sandbox, gate, tools, compress, recovery, loop).

``agentloop/`` sits beside ``core/``, never inside it: ``core/`` is the domain
judgment (what counts as admissible), ``agentloop/`` is infrastructure (how to
drive tools over several turns). This block depends on nothing outside the
standard library and the ports ``core/`` defines -- in particular it does not
import ``adapters/`` or ``sources/``; the assembly point injects those.

Provenance and license: see ``agentloop/README.md``.
"""

__all__: list[str] = []
