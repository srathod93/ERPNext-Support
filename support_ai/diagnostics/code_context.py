# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Code grounding for "why / error" questions. Small local models seldom
call tools on their own, so before the model answers, the turn searches the
custom code for the error text and adds the DocType's customization map.
Only for users with the code access role."""

import json
import re

import frappe

from support_ai.diagnostics.code_search import can_read_code, search_custom_code
from support_ai.diagnostics.customization_map import build_customization_map
from support_ai.diagnostics.vocabulary import detect_targets, extract_entities, tokens_of

MAX_MATCHES = 5
QUOTED_RE = re.compile(r"""["“‘']([^"”’']{12,})["”’']""")
ERROR_LABEL_RE = re.compile(r"\b(?:error|message|exception)\s*[:\-]\s*(.+)", re.IGNORECASE | re.DOTALL)


def code_grounding(message, reference_doctype=None, budget=None):
	"""Text-only form of collect_code_grounding (see there)."""
	return collect_code_grounding(message, reference_doctype, budget)[0]


def collect_code_grounding(message, reference_doctype=None, budget=None):
	"""Code facts for the model: where the error text appears in code, and
	(when the question names a DocType and no form is open) its
	customization map.

	Parameters:
		message (str, required): The user's question.
		reference_doctype (str, optional): DocType of the open form - its map is already in the diagnostic context.
		budget (int, optional): Character budget for the map.

	Returns:
		tuple: (text for a system message or None, source line for the reply or None).
			The source line is set only when the error text matched exactly, so
			the location shown to the user never depends on the model.
	"""
	if not can_read_code():
		return None, None
	parts, source_note = [], None
	rows = search_custom_code(error_text(message), limit=MAX_MATCHES).get("matches") or []
	if rows:
		parts.append(_source_summary(rows[0]))
		source_note = _source_note(rows[0]) if rows[0]["exact"] else None
		parts.append("Where this text appears in this site's custom code:\n" + _format_matches(rows))

	doctype = None if reference_doctype else _single_doctype(message)
	if doctype:
		# With the error's source already found, the map is background - keep it small.
		map_budget = (budget or _default_budget()) // 2 if rows else budget
		customization = build_customization_map(doctype, budget=map_budget, focus=message)
		parts.append(
			f"Customizations of {doctype} on this site (JSON):\n"
			+ json.dumps(customization, default=str, indent=1)
		)
	return "\n\n".join(parts) or None, source_note


def error_text(message):
	"""The part of the message that is the error itself: text after
	"Error:", else a quoted sentence, else the whole message."""
	labelled = ERROR_LABEL_RE.search(message or "")
	if labelled:
		return labelled.group(1).strip()
	quoted = QUOTED_RE.search(message or "")
	return quoted.group(1).strip() if quoted else (message or "").strip()


def _source_summary(match):
	"""One plain sentence naming the most likely source and where to edit
	it - small models tend to repeat the first sentence they are given."""
	where = f"line {match['line']}" + (f", function {match['function']}()" if match["function"] else "")
	if match["source"] in ("Server Script", "Client Script"):
		route = frappe.scrub(match["source"]).replace("_", "-")
		edit = f"It is a {match['source']} saved in the database: open /app/{route}/{match['path']} to change it."
		name = f"the {match['source']} '{match['path']}'"
	else:
		edit = f"It is in the custom app '{match['source']}': edit the file {match['path']} (needs a developer and a deploy)."
		name = f"the file {match['path']} of the app {match['source']}"
	return f"MOST LIKELY SOURCE: this message is raised by {name} ({where}). {edit}"


def _source_note(match):
	"""Markdown line for the top of the reply, with a link for scripts."""
	where = f"line {match['line']}" + (f" in `{match['function']}()`" if match["function"] else "")
	if match["source"] in ("Server Script", "Client Script"):
		route = frappe.scrub(match["source"]).replace("_", "-")
		target = (
			f"{match['source']} [{match['path']}](/app/{route}/{frappe.utils.quote(match['path'], safe='')})"
		)
	else:
		target = f"`{match['path']}` (app {match['source']})"
	return f"**Source of this error:** {target}, {where}."


def _format_matches(rows):
	"""Code matches as plain text blocks."""
	return "\n\n".join(
		f"[{r['source']}] {r['path']}:{r['line']}"
		+ (f" in {r['function']}()" if r["function"] else "")
		+ f"\n{r['snippet']}"
		for r in rows
	)


def _default_budget():
	"""Settings > Code Context Size, or the map's default."""
	from support_ai.diagnostics.customization_map import DEFAULT_BUDGET

	return frappe.get_cached_doc("Support AI Settings").get("code_context_chars") or DEFAULT_BUDGET


def _single_doctype(message):
	"""The one DocType the question names, if exactly one."""
	_emails, _ids, _numbers, rest = extract_entities(message or "")
	targets = detect_targets(rest, tokens_of(rest))
	return targets[0][0] if len(targets) == 1 else None
