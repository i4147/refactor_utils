# Data Files Example

Demonstrates `pyrefactor` preserving data files.

```bash
pyrefactor examples/data_files_example/mypackage -o /tmp/out --copy-data --validate
ls /tmp/out/mypackage/data/config.json
```

With filtering:

```bash
pyrefactor examples/data_files_example/mypackage -o /tmp/out --data-glob "*.json"
```
