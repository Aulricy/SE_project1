"""Bound database integers before routing or SQL parameter binding."""

import re

from werkzeug.routing import IntegerConverter

MAX_DATABASE_INTEGER = 9223372036854775807


def positive_integer(value) -> int:
    """Accept positive decimal values representable by the database's signed integer."""
    if isinstance(value, bool):
        raise ValueError("Boolean values are not identifiers")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        number = int(value)
    else:
        raise ValueError("Expected a bounded decimal integer")
    if not 1 <= number <= MAX_DATABASE_INTEGER:
        raise ValueError("Integer outside the database range")
    return number


class ResourceIDConverter(IntegerConverter):
    """Every integer path identifies an existing positive, bounded database key."""

    regex = r"[0-9]{1,19}"

    def __init__(self, url_map):
        super().__init__(url_map, min=1, max=MAX_DATABASE_INTEGER)
