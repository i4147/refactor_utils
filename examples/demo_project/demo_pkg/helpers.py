"""Small helpers shared by the demo package."""
UTIL = 41


def helper(name):
    """Uppercase helper used by :mod:`demo_pkg.core`."""
    return f"helper({name.upper()})"


def shared():
    return "shared implementation"


if __name__ == "__main__":
    print("helpers main guard:", shared())
