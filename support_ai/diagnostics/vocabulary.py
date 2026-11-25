# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Words and entities the router understands: intent/stop words, DocType
names, aliases and admin-defined synonyms (Support AI Settings), and things
typed verbatim - emails and document IDs - that are checked against the
database before any wording-based guessing."""

import difflib
import re

import frappe

DATA_INTENT_WORDS = {
	"give",
	"show",
	"list",
	"find",
	"get",
	"fetch",
	"search",
	"display",
	"latest",
	"last",
	"recent",
	"newest",
	"oldest",
	"first",
	"id",
	"ids",
	"email",
	"emails",
	"count",
	"many",
	"number",
	"details",
	"which",
	"any",
	"all",
	"records",
	"names",
	"mobile",
	"phone",
	"status",
	"total",
	"amount",
	"top",
	"how",
	"what",
	"who",
	"whose",
	"highest",
	"lowest",
	"biggest",
	"largest",
	"smallest",
	"pending",
	"with",
	"where",
	"having",
	"state",
	"sum",
	"outstanding",
	"stock",
	"items",
	"history",
}

# Wording that means "help me fix/understand something" - always the LLM's job.
DIAGNOSTIC_PHRASES = (
	"why",
	"error",
	"cannot",
	"can t",
	"cant",
	"not able",
	"unable",
	"how do",
	"how to",
	"how can",
	"fix",
	"issue",
	"problem",
	"not working",
	"failed",
	"fails",
	"explain",
	"what is a",
	"what is the use",
)

# Words that are never part of a person/party name filter.
STOP_WORDS = DATA_INTENT_WORDS | {
	"a",
	"an",
	"the",
	"of",
	"for",
	"in",
	"on",
	"at",
	"to",
	"me",
	"my",
	"it",
	"its",
	"is",
	"are",
	"was",
	"and",
	"or",
	"with",
	"from",
	"by",
	"this",
	"that",
	"these",
	"those",
	"please",
	"pls",
	"plz",
	"what",
	"whose",
	"who",
	"how",
	"one",
	"two",
	"three",
	"five",
	"ten",
	"some",
	"do",
	"does",
	"you",
	"can",
	"could",
	"would",
	"tell",
	"about",
	"name",
	"month",
	"year",
	"today",
	"yesterday",
	"week",
	"date",
	"created",
	"made",
	"entry",
	"entries",
	"record",
	"doc",
	"document",
	"documents",
	"no",
	"number",
	"top",
	"only",
	"us",
	"i",
	"want",
	"need",
	"kindly",
	"also",
	"them",
	"their",
	"his",
	"her",
	"between",
	"since",
	"till",
	"until",
	"upto",
	"current",
	"previous",
	"next",
	"quarter",
	"financial",
	"fiscal",
	"fy",
	"days",
	"day",
	"weeks",
	"months",
	"years",
	"past",
	"above",
	"below",
	"over",
	"under",
	"more",
	"less",
	"greater",
	"containing",
	"contains",
	"contain",
	"include",
	"includes",
	"including",
	"we",
	"don",
	"dont",
	"t",
	"s",
	"not",
	"doesn",
	"isn",
	"didn",
	"which",
	"whom",
	"lasted",
	"lates",
	"latset",
	# greetings / filler
	"hello",
	"hi",
	"hey",
	"hii",
	"thanks",
	"thank",
	"dear",
	"sir",
	"madam",
	"good",
	"morning",
	"evening",
	"ok",
	"okay",
	"bro",
	"team",
	"where",
	"has",
	"have",
	"having",
	"be",
	"been",
	"there",
	"value",
	"set",
	"equal",
	"equals",
	"as",
	"like",
	"than",
	"state",
	"type",
	"oldest",
	"earliest",
	"highest",
	"largest",
	"biggest",
	"lowest",
	"smallest",
	"much",
	"so",
	"far",
	"currently",
	"now",
	"just",
	"here",
	"filtered",
}

NEGATION_WORDS = {"not", "don", "dont", "doesn", "isn", "no", "wrong", "didn", "never"}

# Generic words that don't name one DocType. More than one target means
# "ask the user which one" (see router.plan_lookup).
DOCTYPE_ALIASES = {
	"invoice": ["Sales Invoice", "Purchase Invoice"],
	"invoices": ["Sales Invoice", "Purchase Invoice"],
	"bill": ["Purchase Invoice"],
	"bills": ["Purchase Invoice"],
	"order": ["Sales Order", "Purchase Order"],
	"orders": ["Sales Order", "Purchase Order"],
	"payment": ["Payment Entry"],
	"payments": ["Payment Entry"],
	"jv": ["Journal Entry"],
	"jvs": ["Journal Entry"],
	"grn": ["Purchase Receipt"],
	"grns": ["Purchase Receipt"],
	"po": ["Purchase Order"],
	"pos": ["Purchase Order"],
	"dn": ["Delivery Note"],
	"quote": ["Quotation"],
	"quotes": ["Quotation"],
}

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Hyphen/slash-joined code containing a digit: ACC-PINV-2026-17381, SO/24/001.
ID_RE = re.compile(r"(?<![\w@])[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)+(?![\w@])")
PLAIN_DATE_RE = re.compile(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}")
# Long bare numbers (e.g. a Supplier code 810000011) - only ever tried against
# a DocType the user named, never guessed across all of them.
LONG_NUMBER_RE = re.compile(r"(?<![\w-])\d{6,}(?![\w-])")


def tokens_of(text):
	"""Lowercase alphanumeric words of text."""
	return re.findall(r"[a-z0-9]+", (text or "").lower())


def extract_entities(message):
	"""Pulls emails and document IDs out of the message BEFORE it is split
	into words - otherwise "ACC-PINV-2026-17381" becomes a 2026 date filter
	plus a name search for "acc pinv".

	Parameters:
		message (str, required): The user's message.

	Returns:
		tuple: (emails, ids, long_numbers, remaining_text)
	"""
	emails = list(dict.fromkeys(EMAIL_RE.findall(message)))
	text = EMAIL_RE.sub(" ", message)

	ids = [m for m in ID_RE.findall(text) if any(c.isdigit() for c in m) and not PLAIN_DATE_RE.fullmatch(m)]
	ids = list(dict.fromkeys(ids))
	for doc_id in ids:
		text = text.replace(doc_id, " ")

	numbers = list(dict.fromkeys(LONG_NUMBER_RE.findall(text)))
	return emails, ids, numbers, text


def find_document(doc_id, doctype_hint=None):
	"""Resolves a typed ID to (doctype, name) by exact name match. Tries the
	DocType the user named first, then DocTypes whose naming-series prefix
	the ID starts with (longest prefix first).

	Parameters:
		doc_id (str, required): The ID as typed.
		doctype_hint (str, optional): DocType named in the message.

	Returns:
		tuple | None: (doctype, name)
	"""
	upper = doc_id.upper()
	ranked = []
	for dt, prefixes in _naming_prefixes().items():
		best = next((p for p in prefixes if upper.startswith(p)), None)
		if best:
			ranked.append((len(best), dt))
	candidates = ([doctype_hint] if doctype_hint else []) + [
		dt for _n, dt in sorted(ranked, reverse=True) if dt != doctype_hint
	]

	for dt in candidates[:15]:
		try:
			name = frappe.db.get_value(dt, doc_id, "name")
		except Exception:
			continue
		if name:
			return dt, name
	return None


def detect_targets(text, tokens):
	"""[(doctype, words_naming_it)]: an admin synonym, else one exact DocType
	name, else a built-in alias (which may name several DocTypes).

	Parameters:
		text (str, required): Message text with entities removed.
		tokens (list[str], required): Its lowercase words.

	Returns:
		list[tuple]: Possibly empty.
	"""
	synonym = _match_synonym(tokens)
	if synonym:
		return [synonym]
	match = detect_doctype(text)
	if match:
		return [match]
	known = set(doctype_names())
	for t in tokens:
		if t in DOCTYPE_ALIASES:
			return [(dt, t) for dt in DOCTYPE_ALIASES[t] if dt in known]
	return []


def detect_doctype(text):
	"""Longest DocType name in the text as whole words (plural accepted). A
	match that is just a field label of another matched DocType is dropped:
	in "sales invoices with workflow state X", "Workflow State" is a field.

	Parameters:
		text (str, required): Message text.

	Returns:
		tuple | None: (doctype, normalized_words)
	"""
	norm = " " + " ".join(tokens_of(text)) + " "
	matches = []
	for name in doctype_names():
		dt_norm = " ".join(tokens_of(name))
		if dt_norm and any(f" {v} " in norm for v in (dt_norm, dt_norm + "s", dt_norm + "es")):
			matches.append((name, dt_norm))
	if len(matches) > 1:
		labels_by_dt = {
			name: {" ".join(tokens_of(df.label or "")) for df in frappe.get_meta(name).fields}
			for name, _n in matches
		}
		matches = [
			(name, dt_norm)
			for name, dt_norm in matches
			if not any(dt_norm in labels_by_dt[other] for other, _o in matches if other != name)
		] or matches
	return max(matches, key=lambda m: len(m[1])) if matches else None


def previous_doctype(history):
	"""DocType named in the most recent earlier user message, so a follow-up
	("we don't have supplier x@y.com") keeps the same subject.

	Parameters:
		history (list[str], optional): Earlier user messages, oldest first.

	Returns:
		tuple | None: (doctype, normalized_words)
	"""
	for text in reversed(history or []):
		_e, _i, _n, rest = extract_entities(text or "")
		match = detect_targets(rest, tokens_of(rest))
		if len(match) == 1:
			return match[0]
	return None


def is_fuzzy(token, words, cutoff=0.75, min_length=4):
	"""True when token is a likely typo of one of words ("inovoce" ~ "invoice").
	Short tokens are too ambiguous: "test" is 80% similar to "latest"."""
	return len(token) >= min_length and bool(difflib.get_close_matches(token, words, n=1, cutoff=cutoff))


def has_fuzzy_intent(tokens):
	"""True when a word is a typo of a data-intent word ("lasted", "lates")."""
	return any(is_fuzzy(t, ("latest", "recent"), 0.8, min_length=5) for t in tokens)


def doctype_names():
	"""Non-child, non-single DocType names, cached for an hour."""

	def _load():
		"""Reads the DocType names from the database."""
		return frappe.get_all("DocType", filters={"istable": 0, "issingle": 0}, pluck="name")

	try:
		return frappe.cache.get_value("support_ai_routable_doctypes", _load, expires_in_sec=3600)
	except Exception:
		# Redis down shouldn't break lookups - the query itself is cheap.
		return _load()


def _match_synonym(tokens):
	"""(doctype, phrase) for the longest admin-defined synonym in tokens."""
	rows = frappe.get_cached_doc("Support AI Settings").get("synonyms") or []
	best = None
	for row in rows:
		phrase = tokens_of(row.phrase)
		if not phrase or not row.document_type:
			continue
		if contains_phrase(tokens, phrase) or contains_phrase(tokens, [*phrase[:-1], phrase[-1] + "s"]):
			if not best or len(phrase) > len(best[1].split()):
				best = (row.document_type, " ".join(phrase))
	return best


def contains_phrase(tokens, phrase_tokens):
	"""True when phrase_tokens appear consecutively in tokens."""
	n = len(phrase_tokens)
	return bool(n) and any(tokens[i : i + n] == phrase_tokens for i in range(len(tokens) - n + 1))


def _naming_prefixes():
	"""{doctype: [literal prefix, ...]} from every naming series / autoname,
	e.g. "ACC-PINV-.YYYY.-" -> "ACC-PINV-". Cached for an hour."""
	try:
		return frappe.cache.get_value(
			"support_ai_naming_prefixes", _load_naming_prefixes, expires_in_sec=3600
		)
	except Exception:
		return _load_naming_prefixes()


def _load_naming_prefixes():
	"""Builds the {doctype: prefixes} map from DocField, Custom Field,
	Property Setter and DocType.autoname."""
	raw = {}
	sources = (
		("DocField", {"fieldname": "naming_series"}, ["parent", "options"]),
		("Custom Field", {"fieldname": "naming_series"}, ["dt", "options"]),
		("Property Setter", {"field_name": "naming_series", "property": "options"}, ["doc_type", "value"]),
		("DocType", {"autoname": ["like", "%#%"]}, ["name", "autoname"]),
	)
	for doctype, filters, fields in sources:
		for dt, blob in frappe.get_all(doctype, filters, fields, as_list=True):
			raw.setdefault(dt, []).extend(
				(blob or "").replace("format:", "").replace("naming_series:", "").split("\n")
			)

	out = {}
	for dt, options in raw.items():
		# Literal text before the first placeholder (.YYYY. / .#### / {field}).
		prefixes = {re.split(r"[.{#]", o.strip(), maxsplit=1)[0].strip().upper() for o in options}
		prefixes = sorted((p for p in prefixes if len(p) >= 2), key=len, reverse=True)
		if prefixes:
			out[dt] = prefixes
	return out
