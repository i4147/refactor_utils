"""refactor_utils: consolidate python packages into a single file."""

from .core import ConsolidationError, ConsolidationResult, consolidate

__version__ = "2.0.0"
__all__ = ["ConsolidationError", "ConsolidationResult", "consolidate", "__version__"]
