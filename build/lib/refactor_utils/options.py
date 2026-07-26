"""Options dataclass for pyrefactor configuration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Options:
    mode: str
    root: str = "."
    backup: bool = True
    format: bool = False
    verbose: bool = True
    undo: bool = False
    single: bool = True
    target_funcs: str = ""
    target_consts: str = ""
    target_classes: str = ""

    def __post_init__(self):
        # Set default target filenames if not provided
        if not self.target_funcs:
            object.__setattr__(self, "target_funcs", "funcs.py")
        if not self.target_consts:
            object.__setattr__(self, "target_consts", "consts.py")
        if not self.target_classes:
            object.__setattr__(self, "target_classes", "classes.py")
