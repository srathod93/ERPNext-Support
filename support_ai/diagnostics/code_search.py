# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Finds where something lives in this site's customizations: source files
of the installed custom apps, plus Server Scripts and Client Scripts saved
in the database. The main use is "error -> source": paste an error message
and get the file, line and function that raises it.

Only users with the code access role (Support AI Settings, default System
Manager) may search code. Obvious hard-coded secrets are redacted from
snippets."""

import os
import re
import time

import frappe

from support_ai.diagnostics.indexer import get_installed_custom_apps

SOURCE_EXTENSIONS = (".py", ".js", ".html", ".json")
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "dist", "fixtures", "locale", "translations", "tests"}
MAX_FILE_BYTES = 400_000
CONTEXT_LINES = 3
MAX_FRAGMENTS = 24
# A line must hold at least ~3 words of the query to count as a match.
MIN_SCORE = 12
# Lines that raise or show a message are what "where is this error from" wants.
ERROR_LINE_RE = re.compile(
	r"\b(?:throw|msgprint|ValidationError|PermissionError|raise)\b|frappe\.msgprint|frappe\.throw"
)
ERROR_LINE_BONUS = 25
FILE_LIST_TTL = 600
# key = value pairs that look like real credentials, e.g. api_key = "abcd1234..."
SECRET_RE = re.compile(
	r"""((?:api[_-]?key|secret|password|passwd|token|client[_-]?secret|private[_-]?key)\w*\s*[=:]\s*)(["'])[^"'\s]{6,}\2""",
	re.IGNORECASE,
)
FUNCTION_RE = re.compile(
	r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)|^\s*(\w+)\s*(?::|=)\s*(?:async\s+)?function\b|^\s*function\s+(\w+)"
)

# "at" is None until the first load; 0.0 would look fresh on a server whose
# monotonic clock (uptime) is still under FILE_LIST_TTL.
_file_cache = {"at": None, "files": []}


def can_read_code():
	"""True when the current user may let Support AI read source code."""
	role = frappe.get_cached_doc("Support AI Settings").get("code_access_role") or "System Manager"
	return role in frappe.get_roles()


def search_custom_code(query, limit=15):
	"""Searches custom apps and Server/Client Scripts for a text or error
	message. Messages with filled-in values ("Row 3: Amount 500 is wrong")
	still match the template in code ("Row {0}: Amount {1} is wrong"),
	because the longest plain-text fragments are searched as well.

	Parameters:
		query (str, required): Error message, function name or any text.
		limit (int, optional): Max matches to return.

	Returns:
		dict: {"query", "fragments", "matches": [{source, path, line, function, snippet}]} or {"error"}.
	"""
	if not can_read_code():
		return {
			"error": "You do not have permission to search source code (see Support AI Settings > Code Access Role)."
		}
	fragments = search_fragments(query)
	if not fragments:
		return {"error": "Give at least a few words or an error message to search for."}

	matches = _search_files(fragments) + _search_scripts(fragments)
	matches.sort(key=lambda m: (-m["score"], m["path"], m["line"]))
	return {
		"query": query,
		"fragments": fragments,
		"matches": [_public(m) for m in matches[: max(1, int(limit))]],
	}


def search_fragments(query):
	"""Lowercase pieces of the query worth searching. The first is the whole
	text; then runs of words between quotes, numbers and IDs (the parts a
	format string fills in); then 3-word pieces, so a filled-in template
	("Last Name cannot be empty" vs f"{field_name} cannot be empty") and a
	question that wraps the error still find the code."""
	text = re.sub(r"<[^>]+>", " ", query or "").strip()
	if len(text) < 3:
		return []
	pieces = re.split(r"""["'`{}()\[\]]|\b[\w./-]*\d[\w./-]*\b|[:;|]""", text)
	pieces = [" ".join(p.split()) for p in pieces]
	runs = [p for p in pieces if len(p.split()) >= 3 or (len(p) >= 12 and " " not in p)]
	fragments = [" ".join(text.split())] + sorted(runs, key=len, reverse=True) + _shingles(runs or [text])
	return list(dict.fromkeys(f.lower() for f in fragments if len(f) >= 4))[:MAX_FRAGMENTS]


def _shingles(pieces, size=3):
	"""Overlapping runs of `size` words from each piece."""
	out = []
	for piece in pieces:
		words = piece.split()
		out += [" ".join(words[i : i + size]) for i in range(len(words) - size + 1)]
	return out


def redact(text):
	"""Hides values of obvious hard-coded credentials in a snippet."""
	return SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***{m.group(2)}", text)


def _search_files(fragments):
	"""Matches in custom app source files."""
	matches = []
	for app, root, path in _source_files():
		lines = _read_lines(path)
		if lines is None:
			continue
		matches += _matches_in(lines, fragments, source=app, path=os.path.relpath(path, root))
	return matches


def _search_scripts(fragments):
	"""Matches in enabled Server Scripts and Client Scripts (database code)."""
	matches = []
	scripts = [
		("Server Script", r.name, r.script)
		for r in frappe.get_all("Server Script", filters={"disabled": 0}, fields=["name", "script"])
	] + [
		("Client Script", r.name, r.script)
		for r in frappe.get_all("Client Script", filters={"enabled": 1}, fields=["name", "script"])
	]
	for kind, name, script in scripts:
		matches += _matches_in((script or "").splitlines(), fragments, source=kind, path=name)
	return matches


def _matches_in(lines, fragments, source, path):
	"""One match per line that contains a fragment, scored by how much of
	the query the line holds (the full text scores highest)."""
	matches = []
	for number, line in enumerate(lines, 1):
		lowered = line.lower()
		hits = [index for index, f in enumerate(fragments) if f in lowered]
		if not hits:
			continue
		score = sum(len(fragments[i]) for i in hits) + (1000 if 0 in hits else 0)
		if score >= MIN_SCORE and ERROR_LINE_RE.search(line):
			score += ERROR_LINE_BONUS
		if score >= MIN_SCORE:
			matches.append({"source": source, "path": path, "line": number, "score": score, "lines": lines})
	return matches


def _public(match):
	"""Result row with the enclosing function and a redacted snippet."""
	lines, number = match["lines"], match["line"]
	start, end = max(0, number - 1 - CONTEXT_LINES), min(len(lines), number + CONTEXT_LINES)
	snippet = "\n".join(f"{i + 1:>5}  {lines[i]}" for i in range(start, end))
	return {
		"source": match["source"],
		"path": match["path"],
		"line": number,
		# The whole query text is on this line (not just pieces of it).
		"exact": match["score"] >= 1000,
		"function": _enclosing_function(lines, number),
		"snippet": redact(snippet),
	}


def _enclosing_function(lines, number):
	"""Name of the nearest def/class/function above the line, if any."""
	for line in reversed(lines[:number]):
		found = FUNCTION_RE.match(line)
		if found:
			return next(g for g in found.groups() if g)
	return None


def _source_files():
	"""[(app, app_root, path)] of custom app source files, cached for 10 minutes."""
	if _file_cache["at"] is None or time.monotonic() - _file_cache["at"] > FILE_LIST_TTL:
		_file_cache["files"] = list(_walk_custom_apps())
		_file_cache["at"] = time.monotonic()
	return _file_cache["files"]


def _walk_custom_apps():
	"""Yields (app, app_root, path) for every source file of every custom app."""
	for app in get_installed_custom_apps():
		try:
			package = frappe.get_app_path(app)
		except Exception:
			continue
		root = os.path.dirname(package)
		for folder, dirs, files in os.walk(package):
			dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
			yield from ((app, root, os.path.join(folder, f)) for f in files if f.endswith(SOURCE_EXTENSIONS))


def _read_lines(path):
	"""File lines, or None for unreadable / very large files."""
	try:
		if os.path.getsize(path) > MAX_FILE_BYTES:
			return None
		with open(path, encoding="utf-8", errors="ignore") as f:
			return f.read().splitlines()
	except OSError:
		return None
