from dataclasses import dataclass
@dataclass(frozen=True)
class Options:
    root = "."
    backup = True
    format = False
    verbose = True
    undo = False
    single = True
    target_funcs = ""
    target_consts = ""
    target_classes = ""
    def __post_init__(self):
        if not self.target_funcs:
            object.__setattr__(self, "target_funcs", "funcs.py")
        if not self.target_consts:
            object.__setattr__(self, "target_consts", "consts.py")
        if not self.target_classes:
            object.__setattr__(self, "target_classes", "classes.py")
