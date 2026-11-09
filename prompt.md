apply these changes to project
output style:
the pyrefactor cli entry point when run without any flags will produce just one file in `output` folder
name of file will be: the name of folder we are in (Path.cwd().name)

some rules to consider:
- dont extract __all__ as constant value, __all__ should be ignored, no need to emit
- i tested the project in current state, none of constant definitions found in files, emitted to output, fix it
- resolve names: functions/classes/const defs that defined in more than one place & not identical (use content hash to check) & may have same names, the names shoukd be resolved,
- when sorting names that start with _ shoukd appear before alphanumeric names
- in TYPE_CHECKING section: should merge and handle import just like import section at head of file
  check imports under TYPE_CHECKING section, they may be imported normally at head of file too
- remove unnecessary cli flags, from now on, the pyrefactor currently has these flags

usage: pyrefactor [-h] [-o OUTPUT]
                  [-m {single,package,module,flat,split}]
                  [-j WORKERS] [--jobs JOBS]
                  [--executor {process,thread}]
                  [--no-parallel] [--progress]
                  [--progress-bar] [-x GLOB]
                  [--include GLOB] [--copy-data]
                  [--no-copy-data]
                  [--data-glob GLOB]
                  [--entry MODULE]
                  [--entry-keep-all]
                  [--main-name NAME]
                  [--generate-guard]
                  [--keep-all-mains]
                  [--keep-unused-imports]
                  [--imports {prune,keep,strict}]
                  [--respect-all]
                  [--all {ignore,respect,merge,recompute}]
                  [--rename-strategy {prefix,suffix,module_path,error}]
                  [--dedupe {none,safe,aggressive}]
                  [--keep-docstrings]
                  [--strip-docstrings]
                  [--keep-comments]
                  [--strip-comments]
                  [--namespace]
                  [--preserve-modules]
                  [--source-map] [--dry-run]
                  [--check] [--stdout] [--diff]
                  [--overwrite] [--force]
                  [--config PATH] [--pyproject]
                  [--setup-cfg] [--format]
                  [--post-format]
                  [--formatter {black,ruff}]
                  [--validate]
                  [--validate-import] [--strict]
                  [--fail-on-warning]
                  [--max-skipped N]
                  [--allow-syntax-errors]
                  [--log-level {TRACE,DEBUG,INFO,WARNING,ERROR}]
                  [--log-file LOG_FILE]
                  [--version]
                  source



with above changes all modes will be merged in one (no need to -m flag)
--validate-imports, --validate flags should be removed, validation is mandatory
--allow-syntax-error, --max-dkipped, --source-map, --preserve-module,--all,--respect-all,--no-parallel,--no-copy-data flags should be removed
any of them that you think is mandatory, keep it

