please fix these issues:
1. __all__ shouldn't be considered as constant value and be emited.
2. in sorting process, names that start with `_` should appear before names that start with normal alphabet chars
3. imports under `if TYPE CHECKING:` should be merged, and check that imports under `if TYPE_CHECKING:` may be imported earlier as normal import
4. in current stat: classes that their name start with `_` dont appear in __init__.py, also check this situation for constants and functions (constants didn't emited at all)
5. constant definitions that defined in more than one place, but have different values, should be handled properly
after fixing above cases, add proper tests about them.
commit the changes.
