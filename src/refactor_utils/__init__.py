"""pyrefactor — consolidate a python package into a single-file equivalent."""
from .core import (
    ConsolidationError,
    ConsolidationResult,
    consolidate,
    validate_syntax,
)
from .resolver import Plan

__version__ = "2.1.0"

__all__ = [
    "ConsolidationError",
    "ConsolidationResult",
    "Plan",
    "consolidate",
    "validate_syntax",
    "__version__",
]
