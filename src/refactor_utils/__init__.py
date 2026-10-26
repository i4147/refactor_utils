"""refactor_utils: consolidate python packages into a single file."""

from .core import ConsolidationError, ConsolidationResult, consolidate

__version__ = "0.1.0"
__all__ = ["ConsolidationError", "ConsolidationResult", "consolidate", "__version__"]
