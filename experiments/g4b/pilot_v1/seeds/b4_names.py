import re
import unicodedata

RESERVED = {"admin", "root", "support", "mooting"}
_NAME = re.compile(r"[a-z0-9_]{3,16}")


def normalise(name: str) -> str:
    """Lower-case and NFKC-normalise, so look-alike forms compare equal."""
    return unicodedata.normalize("NFKC", name).strip().lower()


def check_username(name: str) -> str:
    """Return the normalised name, or raise ValueError.

    A username is 3-16 characters, each a lower-case letter, a digit or an
    underscore, and is not reserved.
    """
    n = normalise(name)
    if not _NAME.match(n):
        raise ValueError("3-16 of a-z, 0-9 and _")
    if n in RESERVED:
        raise ValueError(f"{n!r} is reserved")
    return n


def suggest(name: str, taken: set[str]) -> str:
    """The first free variant: name, name_2, name_3, ..."""
    base = check_username(name)
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"
