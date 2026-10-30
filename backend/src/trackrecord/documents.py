"""Plain text for filing documents.

The text keeps line structure (paragraphs, list items and table rows on their
own lines, table cells separated by " | ") because the extractor reads tables
of guidance, and because promise quotes are verified against this exact text
by character offset. Quotes, dashes and spaces are normalized to ASCII so a
model's quote can match verbatim.
"""

import re
import unicodedata
from collections.abc import Iterable

from selectolax.lexbor import LexborHTMLParser, LexborNode

PARSER_VERSION = "text-v2"

_LINE = chr(0x2029)  # placeholder for a line break
_CELL = chr(0x241F)  # placeholder for a table cell boundary
_BLOCKS = "p, div, tr, li, h1, h2, h3, h4, h5, h6, table, br, title"

_REPLACEMENTS = {
    0x2018: "'",  # curly single quotes
    0x2019: "'",
    0x201C: '"',  # curly double quotes
    0x201D: '"',
    0x2010: "-",  # hyphen, non-breaking hyphen, en/em dash, minus
    0x2011: "-",
    0x2013: "-",
    0x2014: "-",
    0x2212: "-",
    0x00A0: " ",  # non-breaking and thin spaces
    0x2009: " ",
    0x200B: "",  # zero-width space
}
_TRANSLATE = str.maketrans(_REPLACEMENTS)


def html_to_text(html: bytes | str) -> str:
    source = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    tree = LexborHTMLParser(source)
    for node in tree.css("script, style"):
        node.decompose()
    # Markers go into the DOM, not the HTML source: text between table tags in the
    # source would be moved out of the table by the HTML parser.
    for cell in tree.css("td, th"):
        cell.insert_child(_CELL)
    for block in tree.css(_BLOCKS):
        if block.tag in {"tr", "table"} or not _inside_cell(block):
            block.insert_after(_LINE)
    root = tree.body or tree.root
    raw = root.text(separator=" ") if root is not None else ""
    return normalize_text(raw)


def _inside_cell(node: LexborNode) -> bool:
    """Paragraphs inside a table cell stay on the row's line."""
    parent = node.parent
    while parent is not None:
        if parent.tag in {"td", "th"}:
            return True
        parent = parent.parent
    return False


def normalize_text(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw).translate(_TRANSLATE)
    lines = []
    for line in text.split(_LINE):
        cells = _merge_symbol_cells(re.sub(r"\s+", " ", c).strip() for c in line.split(_CELL))
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _merge_symbol_cells(cells: Iterable[str]) -> list[str]:
    """Financial tables put "$" and "%" in cells of their own: join them to the number."""
    merged: list[str] = []
    prefix = ""
    for cell in cells:
        if not cell:
            continue
        if cell in {"$", "("}:
            prefix += cell
        elif cell in {"%", ")", "%)"} and merged:
            merged[-1] += cell
        else:
            merged.append(prefix + cell)
            prefix = ""
    return merged
