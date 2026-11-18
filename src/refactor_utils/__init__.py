from .core import (
    ConsolidationError,
    ConsolidationResult,
    consolidate,
    validate_syntax,
)
from .resolver import Plan

__version__ = "4.0.1"

__all__ = [
    "ConsolidationError",
    "ConsolidationResult",
    "Plan",
    "consolidate",
    "validate_syntax",
    "__version__",
]
