"""Demo package exercised by the consolidation tests and examples.

This package deliberately mixes patterns the consolidator must handle:
relative imports, a class, module constants, an optional-dependency import
guard, two ``__main__`` guards (merged in the consolidated file), an
identical duplicated function (deduplicated), and data files that are
loaded from disk in package mode and from embedded resources in
consolidated mode.
"""
from .core import Person, greet
from .helpers import UTIL, helper, shared

VERSION = "1.2.3"

__all__ = ["Person", "greet", "helper", "shared", "UTIL", "VERSION"]
