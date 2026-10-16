"""Batch download: expand a URL template with a wildcard into many URLs.

Example: https://example.com/img_*.jpg  from 1 to 100 width 3
         -> img_001.jpg ... img_100.jpg
Letters are supported too: from 'a' to 'z'.
"""
from __future__ import annotations


def expand(pattern: str, start: str, end: str, width: int = 0, wildcard: str = "*") -> list[str]:
    if wildcard not in pattern:
        raise ValueError(f"The address must contain the wildcard '{wildcard}'")
    if start.isdigit() and end.isdigit():
        a, b = int(start), int(end)
        step = 1 if b >= a else -1
        values = [str(n).zfill(width) for n in range(a, b + step, step)]
    elif len(start) == 1 and len(end) == 1 and start.isalpha() and end.isalpha():
        a, b = ord(start), ord(end)
        step = 1 if b >= a else -1
        values = [chr(c) for c in range(a, b + step, step)]
    else:
        raise ValueError("Range must be two numbers or two single letters")
    if len(values) > 100000:
        raise ValueError("Too many URLs (max 100000)")
    return [pattern.replace(wildcard, v) for v in values]
