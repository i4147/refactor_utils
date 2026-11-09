apply these changes to project
1. output must be change like this:
   when user runs pyrefactor in a pkg folder:
       the pkg folder and 2 level upper (the source code may be in src/<pkgname> dir) must be investigated to detect pkg name (search for setup.py,pyproject.toml,setup.cfg,...)
       if not found, use current folder's name (the folder that we are in) as pkg name.
       note that no -m single needed anymore, only input required is path to pkg:
       pyrefactor <pkg path> , if pkg path is not provided, consider it is '.'
       and create `consolidated` subdir in '.' and inside that there should be just one file: <pkgname>.py
       note that we used to create `output` dir with two files (__init__.py,_consolidated.py)  before
       now we create consolidated/<pkgname>.py structure
       the process of extraction is as it was with a change that explained in `2` section
 
2. first we should extract constant values from all over pkg structure, resolve name colisions, and save temporarily to const.py
   do the same for functions and save to func.py, and classes to classes.py
   then extract imports, and dedup, strip local imports (from .x import y, import .x, from . import x) and save optimized imports to
   consolidated/<pkgname>.py, then add contents of const.py,classes.py and func.py (and remove const.py,classes.py.py,func.py)
   i added a python module to <autoall> folder, use that after creating `consolidated/<pkgname>.py to automatically add __all__ section.
   make required changes to autoall to update file inplace
3. write proper tests and let me test over some pkgs and see the results
4. document the project files (if missing) including functions/classes & module docstring, and build html docs and a single file pdf version
5. explain doc building process in docs/README.md (explain steps)

save the helper scripts (bash, python, ...) thst you used during this session under arena folder in root of connected repo
note: all these changes should be saved to a new branch, to compare with main branch


