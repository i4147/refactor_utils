i found a bug in project, in name resolvation section.
first of all, before i forget, this project needs a simple ast validator function, to validate syntax of result code before writing to file
actually there were 3 kind of --validate,--validate-i,ports,... flags before -> i removed them
and commented the mandatory validator in core.py file, coz it was too strict.

and the bug:
i ran pyrefactor .  on a code base with two files, i added them to mytest1 folder
there were 2 Tokenizer class, some other dup names.
see the result.py yourself,

second class renamed to:

class 2_Tokenenizer:
  ...

  but python dont accept this name

i hope yoy check other renaming logic, and consider python valifity of renamed ones (new names)


another issue:
in current state of 0 project, module docstr8ngs are considered as const and will be written to consolidated file,
new behaviour:
  module docstrings of source file will be prunned ( will not be written to result code)
