# Rules for AI agents
- Only add or edit docstrings. Never change function bodies, signatures, or imports.
- Use Google-style docstrings.
- Preserve any existing docstring content; extend rather than replace.
- Run `ruff check --select D .` and `pytest -q` before committing.
- One commit per module.