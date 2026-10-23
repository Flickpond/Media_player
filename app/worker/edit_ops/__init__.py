"""Edit operations, one module each. See registry.py for how to add one.

Importing this package registers every operation's filters; a new module is
added to the import line below.
"""

from app.worker.edit_ops import (  # noqa: F401 (registers the filters)
    adjust,
    crop,
    flip,
    preset,
    rotate,
    scale,
    speed,
)
from app.worker.edit_ops.registry import (
    FILTERS,
    FilterChain,
    FilterStep,
    filter_chain,
    register_filter,
)

__all__ = ["FILTERS", "FilterChain", "FilterStep", "filter_chain", "register_filter"]
