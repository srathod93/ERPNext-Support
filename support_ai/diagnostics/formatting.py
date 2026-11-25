# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Markdown building blocks for router answers: clickable document links,
tables and the "Source:" line. The chat renderer understands exactly this
subset (see support_ai_message_renderer.js)."""

import frappe


def doc_link(doctype, name):
	"""Markdown link that opens the document's form, e.g.
	[ACC-PINV-2026-17381](/app/purchase-invoice/ACC-PINV-2026-17381).

	Parameters:
		doctype (str, required): DocType of the document.
		name (str, required): Document name.

	Returns:
		str: Markdown link, or the escaped name when either part is missing.
	"""
	if not doctype or not name:
		return escape_cell(name or "")
	route = frappe.scrub(doctype).replace("_", "-")
	return f"[{escape_cell(name)}](/app/{route}/{frappe.utils.quote(str(name), safe='')})"


def markdown_table(header, rows):
	"""Pipe table text from a header list and a list of row lists.

	Parameters:
		header (list[str], required): Column titles.
		rows (list[list], required): Cell values; already-formatted links are kept as is.

	Returns:
		str: Markdown table.
	"""
	lines = ["| " + " | ".join(escape_cell(h) for h in header) + " |", "|" + "---|" * len(header)]
	for row in rows:
		lines.append("| " + " | ".join(_cell_text(c) for c in row) + " |")
	return "\n".join(lines)


def source_line(source, detail=""):
	"""Italic 'Source: ...' footer that tells users where an answer came from.

	Parameters:
		source (str, required): DocType(s) / report read.
		detail (str, optional): Filters or scope applied.

	Returns:
		str: Markdown line.
	"""
	return f"\n_Source: {source}{' · ' + detail if detail else ''}_"


def escape_cell(value):
	"""Text safe inside a table cell: no pipes or newlines."""
	return str(value if value is not None else "").replace("|", "/").replace("\n", " ").strip()


def _cell_text(value):
	"""Cell text, leaving markdown links (already escaped) untouched."""
	text = str(value if value is not None else "")
	if text.startswith("[") and "](/app/" in text:
		return text
	return escape_cell(text)
