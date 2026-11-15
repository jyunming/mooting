import os


def safe_join(root: str, requested: str) -> str:
    """Resolve `requested` under `root`, refusing anything that escapes it.

    The static-file handler calls this with `requested` taken straight from
    the URL path, so it has to hold against `..` and absolute paths.
    """
    root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root, requested))
    if not path.startswith(root):
        raise PermissionError(f"outside the served directory: {requested}")
    return path


def serve(root: str, requested: str) -> bytes:
    with open(safe_join(root, requested), "rb") as f:
        return f.read()
