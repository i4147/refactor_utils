"""``python -m autoall`` entry point (delegates to :func:`autoall.main`)."""

from . import main

__all__ = ["main"]
if __name__ == "__main__":
    raise SystemExit(main())
