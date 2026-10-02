"""bobanana6 package root.

Only the stable side (`bobanana6.core`) exists in this unit. Everything that
touches the environment (models, network, contract parsing, the CLI assembly
point) is a later unit and must never be imported from here.
"""

__all__ = ["core"]
