"""Small deterministic metrics; no model loading and no semantic text rewriting."""
from __future__ import annotations
import re
import unicodedata
from html.parser import HTMLParser


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []
    def handle_data(self, data):
        self.parts.append(data)


def plain(value: str) -> str:
    if re.search(r"<(?:table|tr|td|th|p|div)\b", value, re.I):
        parser = _Text(); parser.feed(value); return '\n'.join(parser.parts)
    return value


def normalize(value: str) -> str:
    return ''.join(unicodedata.normalize('NFC', value).split())


def character_error_rate(expected: str, observed: str) -> float:
    left, right = normalize(expected), normalize(plain(observed))
    if not left:
        return 0.0 if not right else 1.0
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1] / len(left)


def result_text(result: dict) -> str:
    return '\n'.join(plain(str(block.get('text') or '')) for page in result.get('pages') or [] for block in page.get('blocks') or [])


def table_cells(result: dict) -> list[str]:
    class Cells(HTMLParser):
        def __init__(self):
            super().__init__(); self.depth = 0; self.current = []; self.cells = []
        def handle_starttag(self, tag, attrs):
            if tag in {'td', 'th'}: self.depth += 1; self.current = []
        def handle_data(self, data):
            if self.depth: self.current.append(data)
        def handle_endtag(self, tag):
            if tag in {'td', 'th'} and self.depth:
                self.cells.append(normalize(''.join(self.current))); self.depth -= 1
    parser = Cells()
    for page in result.get('pages') or []:
        for block in page.get('blocks') or []:
            parser.feed(str(block.get('text') or ''))
    return parser.cells