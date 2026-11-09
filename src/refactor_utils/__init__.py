"""refactor_utils: consolidate python packages into a single file."""

from .core import (
    ConsolidationError,
    ConsolidationResult,
    consolidate,
    consolidate_package,
)

__version__ = "1.5.0"
__all__ = [
    "ConsolidationError",
    "ConsolidationResult",
    "consolidate",
    "consolidate_package",
    "__version__",
]
