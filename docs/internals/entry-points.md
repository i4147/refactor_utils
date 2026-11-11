# Entry points

## `main` goes last

A function named `main` that nothing else depends on, and every
`if __name__ == "__main__":` guard, are placed in the final *Entry points*
section.

## Guards are merged

Several modules often have their own guard. Written one after the other, they
would all run, in an order nobody chose. They are merged into a single guard
whose body runs each original entry in module order, annotated with its
origin:

```python
if __name__ == "__main__":
    # from pkg.cli
    main()

    # from pkg.tools.export
    export_all()
```

Guards with an identical body are emitted once. A warning reminds you that
several entry points were merged.

## Choosing entries

| Flag | Effect |
|------|--------|
| `--entry MODULE` | keep only the guard of this module (repeatable; matches the full dotted name or its last part) |
| `--entry-keep-all` | keep every guard even with `--entry` |
| `--main-name NAME` | rename `main` to `NAME` |
| `--generate-guard` | when no guard exists but a `main` function does, add one |

`--main-name` must be a valid Python identifier and not a keyword. When
several modules define `main`, the first one gets `NAME` and the others
`NAME_<module_prefix>`.
