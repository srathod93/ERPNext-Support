# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Deterministic routing for "find/list/give me the id" questions.

Small CPU-friendly local models (1.5B-3B) are unreliable at tool calling -
asked "give the purchase invoice id" they answer from nothing instead of
calling lookup_records. So for plain data questions we don't depend on the
model at all: detect the DocType, a month/year date range and a name filter
from the text here, run lookup_records (which goes through frappe.get_list,
so the asking user's permissions apply), and format the real rows. Anything
that doesn't look like a data question still goes to the LLM as before."""

import calendar
import datetime
import difflib
import re

import frappe
from frappe.utils import cint, fmt_money, getdate, nowdate

from support_ai.diagnostics.lookup import MAX_LIMIT, lookup_records

DATA_INTENT_WORDS = {
	"give", "show", "list", "find", "get", "fetch", "search", "display", "latest", "last", "recent",
	"newest", "oldest", "first", "id", "ids", "email", "emails", "count", "many", "number", "details",
	"which", "any", "all", "records", "names", "mobile", "phone", "status", "total", "amount", "top",
	"how", "what", "who", "whose", "highest", "lowest", "biggest", "largest", "smallest", "pending",
	"with", "where", "having", "state",
}

# Wording that means "help me fix/understand something" - always the LLM's job.
DIAGNOSTIC_PHRASES = (
	"why", "error", "cannot", "can t", "cant", "not able", "unable", "how do", "how to", "how can",
	"fix", "issue", "problem", "not working", "failed", "fails", "explain", "what is a", "what is the use",
)

# Words that are never part of a person/party name filter.
STOP_WORDS = DATA_INTENT_WORDS | {
	"a", "an", "the", "of", "for", "in", "on", "at", "to", "me", "my", "it", "its", "is", "are", "was",
	"and", "or", "with", "from", "by", "this", "that", "these", "those", "please", "pls", "plz", "what",
	"whose", "who", "how", "one", "two", "three", "five", "ten", "some", "do", "does", "you", "can",
	"could", "would", "tell", "about", "name", "month", "year", "today", "yesterday", "week", "date",
	"created", "made", "entry", "entries", "record", "doc", "document", "documents", "no", "number",
	"top", "only", "us", "i", "want", "need", "kindly", "also", "them", "their", "his", "her",
	"between", "since", "till", "until", "upto", "current", "previous", "next",
	"we", "don", "dont", "t", "s", "not", "doesn", "isn", "didn", "which", "whom", "lasted", "lates", "latset",
	# greetings / filler
	"hello", "hi", "hey", "hii", "thanks", "thank", "dear", "sir", "madam", "good", "morning", "evening",
	"ok", "okay", "bro", "team", "where", "has", "have", "having", "be", "been", "there", "value", "set",
	"equal", "equals", "as", "like", "than", "state", "type", "oldest", "earliest", "highest", "largest",
	"biggest", "lowest", "smallest", "much", "so", "far", "currently", "now", "just",
}

# Generic words that don't name one DocType: answer for each listed one.
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
}

# Fields whose values the user names directly ("overdue invoices").
VALUE_FIELDS = ("status", "workflow_state")

MONTHS = [m.lower() for m in calendar.month_name[1:]]
MONTH_ABBR = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}

DATE_FIELD_PREFERENCE = ("posting_date", "transaction_date", "date", "bill_date", "schedule_date")

# Intent words that can also legitimately name a column to show.
FIELD_HINT_WORDS = {"email", "emails", "mobile", "phone", "status", "total", "amount"}

NON_VALUE_FIELDTYPES = {"Section Break", "Column Break", "Tab Break", "Table", "Table MultiSelect", "HTML", "Button", "Fold"}

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10}


def _doctype_names():
	def _load():
		return frappe.get_all("DocType", filters={"istable": 0, "issingle": 0}, pluck="name")

	try:
		return frappe.cache.get_value("support_ai_routable_doctypes", _load, expires_in_sec=3600)
	except Exception:
		# Redis down shouldn't break lookups - the query itself is cheap.
		return _load()


def _tokens(text):
	return re.findall(r"[a-z0-9]+", text.lower())


# --- Entities typed verbatim: emails and document IDs ----------------------------
#
# These must be pulled out of the message BEFORE it is split into words -
# otherwise "ACC-PINV-2026-17381" turns into a 2026 date filter plus a name
# search for "acc pinv", and an email turns into "api user jfl erpnext com".

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Hyphen/slash-joined code containing a digit: ACC-PINV-2026-17381, SO/24/001.
ID_RE = re.compile(r"(?<![\w@])[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)+(?![\w@])")
PLAIN_DATE_RE = re.compile(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}")
# Long bare numbers (e.g. a Supplier code 810000011) - only ever tried against
# a DocType the user named, never guessed across all of them.
LONG_NUMBER_RE = re.compile(r"(?<![\w-])\d{6,}(?![\w-])")


def _extract_entities(message):
	"""Returns (emails, ids, numbers, remaining_text)."""
	emails = list(dict.fromkeys(EMAIL_RE.findall(message)))
	text = EMAIL_RE.sub(" ", message)

	ids = []
	for m in ID_RE.findall(text):
		if any(c.isdigit() for c in m) and not PLAIN_DATE_RE.fullmatch(m):
			ids.append(m)
	ids = list(dict.fromkeys(ids))
	for i in ids:
		text = text.replace(i, " ")

	numbers = list(dict.fromkeys(LONG_NUMBER_RE.findall(text)))
	return emails, ids, numbers, text


def _naming_prefixes():
	"""{doctype: [literal prefix, ...]} from every naming series / autoname,
	e.g. "ACC-PINV-.YYYY.-" -> "ACC-PINV-". Used to tell which DocType a
	typed ID belongs to without probing every table."""

	def _load():
		raw = {}
		for parent, options in frappe.get_all(
			"DocField", {"fieldname": "naming_series"}, ["parent", "options"], as_list=True
		):
			raw.setdefault(parent, []).append(options or "")
		for dt, options in frappe.get_all(
			"Custom Field", {"fieldname": "naming_series"}, ["dt", "options"], as_list=True
		):
			raw.setdefault(dt, []).append(options or "")
		for dt, value in frappe.get_all(
			"Property Setter", {"field_name": "naming_series", "property": "options"}, ["doc_type", "value"], as_list=True
		):
			raw.setdefault(dt, []).append(value or "")
		for dt, autoname in frappe.get_all("DocType", {"autoname": ["like", "%#%"]}, ["name", "autoname"], as_list=True):
			raw.setdefault(dt, []).append((autoname or "").replace("format:", "").replace("naming_series:", ""))

		out = {}
		for dt, option_blobs in raw.items():
			prefixes = set()
			for blob in option_blobs:
				for option in blob.split("\n"):
					# Literal text before the first placeholder (.YYYY. / .#### / {field}).
					prefix = re.split(r"[.{#]", option.strip(), maxsplit=1)[0].strip().upper()
					if len(prefix) >= 2:
						prefixes.add(prefix)
			if prefixes:
				out[dt] = sorted(prefixes, key=len, reverse=True)
		return out

	try:
		return frappe.cache.get_value("support_ai_naming_prefixes", _load, expires_in_sec=3600)
	except Exception:
		return _load()


def find_document(doc_id, doctype_hint=None):
	"""Resolves a typed ID to (doctype, name) by exact name match, or None.
	Tries the DocType the user named first, then DocTypes whose naming
	series prefix the ID starts with (longest prefix first)."""
	candidates = []
	if doctype_hint:
		candidates.append(doctype_hint)
	upper = doc_id.upper()
	ranked = []
	for dt, prefixes in _naming_prefixes().items():
		best = next((p for p in prefixes if upper.startswith(p)), None)
		if best:
			ranked.append((len(best), dt))
	candidates += [dt for _n, dt in sorted(ranked, reverse=True) if dt != doctype_hint]

	for dt in candidates[:15]:
		try:
			name = frappe.db.get_value(dt, doc_id, "name")
		except Exception:
			continue
		if name:
			return dt, name
	return None


def _user_for_email(email):
	return frappe.db.get_value("User", {"name": email}, ["name", "full_name"], as_dict=True) or frappe.db.get_value(
		"User", {"email": email}, ["name", "full_name"], as_dict=True
	)


def _email_fields(meta):
	return [
		df.fieldname
		for df in meta.fields
		if df.fieldtype == "Data" and (df.options == "Email" or "email" in df.fieldname)
	]


def _is_fuzzy(token, words, cutoff=0.75):
	return len(token) >= 4 and bool(difflib.get_close_matches(token, words, n=1, cutoff=cutoff))


def _detect_doctype(text):
	"""Longest DocType name appearing in the text as whole words, also
	accepting a plural ("purchase invoices") and ignoring case. A match that
	is really just a field label of another matched DocType is dropped - in
	"sales invoices with workflow state X", "Workflow State" is Sales
	Invoice's field, not the DocType being asked about."""
	norm = " " + " ".join(_tokens(text)) + " "
	matches = []
	for name in _doctype_names():
		dt_norm = " ".join(_tokens(name))
		if dt_norm and any(f" {v} " in norm for v in (dt_norm, dt_norm + "s", dt_norm + "es")):
			matches.append((name, dt_norm))
	if len(matches) > 1:
		labels_by_dt = {
			name: {" ".join(_tokens(df.label or "")) for df in frappe.get_meta(name).fields}
			for name, _n in matches
		}
		matches = [
			(name, dt_norm)
			for name, dt_norm in matches
			if not any(dt_norm in labels_by_dt[other] for other, _o in matches if other != name)
		] or matches
	return max(matches, key=lambda m: len(m[1])) if matches else None


def _match_month(token):
	if token in MONTH_ABBR and len(token) >= 3:
		return MONTH_ABBR[token]
	if token in MONTHS:
		return MONTHS.index(token) + 1
	if len(token) >= 5 and token not in STOP_WORDS:
		# Typo tolerance, e.g. "augest" -> august, "febuary" -> february. Same
		# first letter and 5+ chars, so ordinary words ("many") aren't months.
		close = difflib.get_close_matches(token, MONTHS, n=1, cutoff=0.75)
		if close and close[0][0] == token[0]:
			return MONTHS.index(close[0]) + 1
	return None


def _detect_date_range(tokens):
	"""Returns ((from_date, to_date), label, consumed_tokens) or (None, None, set())."""
	today = getdate(nowdate())
	joined = " ".join(tokens)
	years = [cint(t) for t in tokens if re.fullmatch(r"(19|20)\d\d", t)]
	year = years[0] if years else None

	if "today" in tokens:
		return (today, today), "today", {"today"}
	if "yesterday" in tokens:
		d = today - datetime.timedelta(days=1)
		return (d, d), "yesterday", {"yesterday"}
	if "this month" in joined or "current month" in joined:
		start = today.replace(day=1)
		return (start, today), "this month", {"this", "current", "month"}
	if "last month" in joined or "previous month" in joined:
		end = today.replace(day=1) - datetime.timedelta(days=1)
		return (end.replace(day=1), end), "last month", {"last", "previous", "month"}

	for t in tokens:
		month = _match_month(t)
		if month:
			y = year or today.year
			last_day = calendar.monthrange(y, month)[1]
			start, end = datetime.date(y, month, 1), datetime.date(y, month, last_day)
			return (start, end), f"{calendar.month_name[month]} {y}", {t, str(y)}

	if year:
		return (datetime.date(year, 1, 1), datetime.date(year, 12, 31)), str(year), {str(year)}

	return None, None, set()


def _date_field(meta):
	fieldnames = {df.fieldname for df in meta.fields}
	for f in DATE_FIELD_PREFERENCE:
		if f in fieldnames:
			return f
	return "creation"


def _name_search_field(meta, doctype):
	fieldnames = {df.fieldname for df in meta.fields}
	if meta.title_field and meta.title_field in fieldnames:
		return meta.title_field
	for f in ("full_name", frappe.scrub(doctype) + "_name", "title", "subject"):
		if f in fieldnames:
			return f
	return "name"


def _detect_limit(tokens):
	for i, t in enumerate(tokens):
		if t in ("latest", "lasted", "last", "top", "recent", "first") and i + 1 < len(tokens):
			nxt = tokens[i + 1]
			if nxt.isdigit():
				return min(cint(nxt), MAX_LIMIT)
			if nxt in NUMBER_WORDS:
				return NUMBER_WORDS[nxt]
	for t in tokens:
		# A bare count like "show me 2 draft invoices" (years excluded).
		if t.isdigit() and 0 < cint(t) <= 1000 and not re.fullmatch(r"(19|20)\d\d", t):
			return min(cint(t), MAX_LIMIT)
	if "any one" in " ".join(tokens) or "anyone" in tokens or "any" in tokens:
		return 1
	if any(t in tokens for t in ("all", "list", "ids", "records")):
		return 10
	return 5


def _value_options(meta, doctype, fieldname):
	"""Allowed values for a status-like field: Select options, or the active
	Workflow's states for workflow_state (a Link, so it has no options)."""
	df = meta.get_field(fieldname)
	if not df:
		return []
	if df.fieldtype == "Select":
		return [o for o in (df.options or "").split("\n") if o.strip()]
	if fieldname == "workflow_state":
		workflow = frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1}, "name")
		if workflow:
			return list(dict.fromkeys(frappe.get_all("Workflow Document State", {"parent": workflow}, pluck="state")))
	return []


def _contains_phrase(tokens, phrase_tokens):
	n = len(phrase_tokens)
	return n and any(tokens[i : i + n] == phrase_tokens for i in range(len(tokens) - n + 1))


def _detect_targets(message, tokens):
	"""[(doctype, words_naming_it)] - one exact DocType, else an alias."""
	match = _detect_doctype(message)
	if match:
		return [match]
	known = set(_doctype_names())
	for t in tokens:
		if t in DOCTYPE_ALIASES:
			return [(dt, t) for dt in DOCTYPE_ALIASES[t] if dt in known]
	return []


def previous_doctype(history):
	"""DocType named in the most recent earlier user message, so a follow-up
	like "we don't have supplier x@y.com" keeps talking about Purchase
	Invoice. history: earlier user messages, oldest first."""
	for text in reversed(history or []):
		_e, _i, _n, rest = _extract_entities(text or "")
		match = _detect_doctype(rest)
		if match:
			return match
	return None


def plan_lookup(message, history=None):
	"""Returns a list of lookup plans (one per DocType), or None if this
	doesn't look like a plain data question - the caller then uses the LLM.

	Anything typed verbatim that can be checked against the database (a
	document ID, an email) is, before any wording-based guessing."""
	emails, ids, numbers, rest = _extract_entities(message)
	tokens = _tokens(rest)
	has_entity = bool(emails or ids)

	if not has_entity and (not tokens or not (set(tokens) & DATA_INTENT_WORDS) and not _has_fuzzy_intent(tokens)):
		return None
	padded = " " + " ".join(tokens) + " "
	if not has_entity and any(f" {p} " in padded for p in DIAGNOSTIC_PHRASES):
		return None

	targets = _detect_targets(rest, tokens)

	# 1. Exact document IDs: "ACC-PINV-2026-17381", or a long number when a
	#    DocType was named ("supplier 810000011").
	hint = targets[0][0] if len(targets) == 1 else None
	doc_plans, missing = [], []
	for doc_id in ids + (numbers if hint else []):
		found = find_document(doc_id, hint)
		if found:
			doc_plans.append(_document_plan(found[0], found[1], tokens))
		elif doc_id in ids:
			missing.append(doc_id)
	if doc_plans or missing:
		return doc_plans + [{"kind": "missing_id", "doc_id": i, "doctype": hint} for i in missing]

	# 2. Emails: filter the list by who created it (or an email field). A
	#    correction ("we don't have supplier x@y.com") keeps the earlier
	#    DocType rather than switching to the one it says is wrong.
	if emails:
		ctx = previous_doctype(history)
		is_correction = bool(set(tokens) & NEGATION_WORDS)
		if ctx and (not targets or (is_correction and ctx[0] not in {t[0] for t in targets})):
			targets = [ctx]
		if not targets:
			return [{"kind": "user_info", "email": e} for e in emails]

	if not targets:
		return None
	return [_plan_for(doctype, dt_norm, rest, tokens, emails=emails) for doctype, dt_norm in targets]


NEGATION_WORDS = {"not", "don", "dont", "doesn", "isn", "no", "wrong", "didn", "never"}


def _has_fuzzy_intent(tokens):
	"""True when a word is a typo of a data-intent word ("lasted", "lates")."""
	return any(_is_fuzzy(t, ("latest", "show", "give", "list", "find", "recent"), 0.8) for t in tokens)


def _document_plan(doctype, name, tokens):
	"""Plan for showing one exact document the user named by ID."""
	return {"kind": "document", "doctype": doctype, "name": name, "tokens": tokens}


def _plan_for(doctype, dt_norm, message, tokens, emails=None):
	"""Builds a list/count plan for one DocType from the message wording."""
	meta = frappe.get_meta(doctype)
	joined = " ".join(tokens)

	dt_words = dt_norm.split()
	consumed = set(dt_words) | {w + "s" for w in dt_words} | {w + "es" for w in dt_words}
	# Typos of the DocType's own words ("inovoce") or of intent words ("lasted").
	consumed |= {t for t in tokens if _is_fuzzy(t, dt_words) or _is_fuzzy(t, ("latest", "recent"), 0.8)}

	filters = {}
	labels = []
	error = None
	show_owner = bool({"who", "owner", "created", "creator", "made"} & set(tokens))

	for email in emails or []:
		user = _user_for_email(email)
		email_fields = _email_fields(meta)
		if user:
			filters["owner"] = user.name
			labels.append(f"Created By = {user.full_name or user.name} ({user.name})")
			show_owner = True
		elif email_fields:
			filters[email_fields[0]] = ["like", f"%{email}%"]
			labels.append(f"{meta.get_label(email_fields[0])} = {email}")
		else:
			error = f"'{email}' is not a User in this system, and {doctype} has no email field to search."

	# Status / workflow state named in the text: longest matching value wins,
	# so "partly paid" beats "paid" and "overdue and discounted" beats "overdue".
	value_matches = []
	for fieldname in VALUE_FIELDS:
		for option in _value_options(meta, doctype, fieldname):
			opt_tokens = _tokens(option)
			if _contains_phrase(tokens, opt_tokens):
				value_matches.append((len(opt_tokens), fieldname, option, opt_tokens))
	if value_matches:
		_n, fieldname, option, opt_tokens = max(value_matches, key=lambda m: m[0])
		filters[fieldname] = option
		consumed |= set(opt_tokens) | set(_tokens(meta.get_label(fieldname)))
		labels.append(f"{meta.get_label(fieldname)} = {option}")

	date_field = None
	date_range, date_label, date_tokens = _detect_date_range(tokens)
	consumed |= date_tokens
	if date_range:
		date_field = _date_field(meta)
		filters[date_field] = ["between", [str(date_range[0]), str(date_range[1])]]
		labels.append(f"{meta.get_label(date_field) if date_field != 'creation' else 'Created'} in {date_label}")

	# Requested columns: a word that names a field, e.g. "email", "status".
	# Per word, take the single best field - an exact fieldname match, else
	# the shortest fieldname/label containing it - so "email" means `email`,
	# not `send_welcome_email` too.
	field_words = [
		t for t in tokens
		if len(t) > 2 and t not in consumed and (t not in STOP_WORDS or t in FIELD_HINT_WORDS)
	]
	requested_fields = []
	for t in field_words:
		candidates = [
			df for df in meta.fields
			if df.fieldtype not in NON_VALUE_FIELDTYPES and not df.hidden
			and (t in df.fieldname.split("_") or t in _tokens(df.label or ""))
		]
		if not candidates:
			continue
		best = min(candidates, key=lambda df: (df.fieldname != t, len(df.fieldname.split("_"))))
		if best.fieldname not in requested_fields:
			requested_fields.append(best.fieldname)
		consumed.add(t)

	leftover = [
		t for t in tokens
		if t not in STOP_WORDS and t not in consumed and not t.isdigit() and _match_month(t) is None
	]
	if emails:
		# The email already identifies who/what - other words are filler.
		leftover = []

	asked_for_status = bool({"status", "state"} & set(tokens)) or "pending" in tokens
	search_field = None
	if leftover and asked_for_status and not value_matches:
		# They asked for a status we don't have - say so and list the real
		# ones, rather than silently turning it into a name search.
		wanted = " ".join(t for t in tokens if t not in consumed and t not in ("status", "state", "with", "is"))
		wanted = re.findall(r"[\"'\u201c\u2018]([^\"'\u201d\u2019]+)[\"'\u201d\u2019]", message) or [wanted]
		lines = [f"'{wanted[0].strip()}' is not a status of {doctype}."]
		for fieldname in VALUE_FIELDS:
			options = _value_options(meta, doctype, fieldname)
			if options:
				lines.append(f"Valid {meta.get_label(fieldname)} values: {', '.join(options)}")
		error = "\n".join(lines)
	elif leftover:
		search_field = _name_search_field(meta, doctype)
		filters[search_field] = ["like", "%" + "%".join(leftover) + "%"]
		labels.append(f"{meta.get_label(search_field) if search_field != 'name' else 'ID'} like '{' '.join(leftover)}'")

	order_by = "creation desc"
	if {"oldest", "earliest"} & set(tokens):
		order_by = "creation asc"
	elif meta.has_field("grand_total") and {"highest", "largest", "biggest"} & set(tokens):
		order_by = "grand_total desc"
	elif meta.has_field("grand_total") and {"lowest", "smallest"} & set(tokens):
		order_by = "grand_total asc"

	is_count = "count" in tokens or any(p in joined for p in ("how many", "number of", "total number"))

	return {
		"kind": "list",
		"doctype": doctype,
		"filters": filters,
		"limit": _detect_limit(tokens),
		"order_by": order_by,
		"is_count": is_count,
		"error": error,
		"requested_fields": requested_fields[:4],
		"label": ", ".join(labels),
		"date_field": date_field,
		"value_field": value_matches and max(value_matches, key=lambda m: m[0])[1],
		"name_words": leftover if search_field else [],
		"name_field": search_field,
		"show_owner": show_owner,
	}


def run_planned_lookup(plans):
	"""Runs each plan and returns one plain-text answer built from real rows."""
	return "\n\n".join(_run_one(plan) for plan in plans)


def _count(plan):
	doctype = plan["doctype"]
	if not frappe.has_permission(doctype, "read"):
		return f"You do not have permission to read {doctype}."
	# Dict-style aggregate: still a frappe.get_list, so permissions apply.
	count = frappe.get_list(doctype, filters=plan["filters"], fields=[{"COUNT": "*", "as": "count"}])[0]["count"]
	where = f" ({plan['label']})" if plan["label"] else ""
	return f"{doctype}: {count:,} record{'s' if count != 1 else ''}{where}."


# Fields worth showing for one document, in order, when present.
DOCUMENT_FIELDS = (
	"supplier", "supplier_name", "customer", "customer_name", "party_type", "party", "party_name",
	"employee", "employee_name", "company", "posting_date", "transaction_date", "bill_no", "bill_date",
	"due_date", "status", "workflow_state", "grand_total", "rounded_total", "outstanding_amount",
	"paid_amount", "total_debit", "voucher_type", "remarks",
)
DOCSTATUS_LABELS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}


def _user_label(user):
	"""'Full Name (user@id)' for a User ID, or the ID if it has no name."""
	if not user:
		return ""
	full_name = frappe.db.get_value("User", user, "full_name")
	return f"{full_name} ({user})" if full_name and full_name != user else user


def _format_value(meta, fieldname, value, row):
	"""Display text for one field value (money in the document's currency)."""
	df = meta.get_field(fieldname)
	if df and df.fieldtype == "Currency" and value not in (None, ""):
		return fmt_money(value, currency=row.get("currency"))
	if isinstance(value, str) and len(value) > 200:
		return value[:200] + "..."
	return value


def _run_document(plan):
	"""Details of one exact document the user named by ID, permission-checked."""
	doctype, name = plan["doctype"], plan["name"]
	if not frappe.has_permission(doctype, "read", doc=name):
		return f"{doctype} {name} exists, but you do not have permission to read it."

	meta = frappe.get_meta(doctype)
	tokens = plan["tokens"]
	# Extra fields the user named, e.g. "supplier name", "bill no", "tax".
	requested = []
	for t in tokens:
		if len(t) <= 2 or t in STOP_WORDS:
			continue
		for df in meta.fields:
			if df.fieldtype in NON_VALUE_FIELDTYPES or df.hidden:
				continue
			if t in df.fieldname.split("_") and df.fieldname not in requested:
				requested.append(df.fieldname)
				break

	fields = [f for f in (meta.title_field, *DOCUMENT_FIELDS, *requested) if f and meta.has_field(f)]
	fields = list(dict.fromkeys(fields))
	if meta.has_field("currency"):
		fields.append("currency")
	row = frappe.get_list(
		doctype,
		filters={"name": name},
		fields=["name", "owner", "creation", "modified", "modified_by", "docstatus", *fields],
		limit_page_length=1,
	)
	if not row:
		return f"{doctype} {name} exists, but you do not have permission to read it."
	row = row[0]

	lines = [f"{doctype} {name}:"]
	for f in fields:
		df = meta.get_field(f)
		if f == "currency" or row.get(f) in (None, "") or (df.fieldtype in ("Currency", "Float") and not row[f]):
			continue
		lines.append(f"- {meta.get_label(f)}: {_format_value(meta, f, row[f], row)}")
	if meta.is_submittable:
		lines.append(f"- Document Status: {DOCSTATUS_LABELS.get(row.docstatus, row.docstatus)}")
	lines.append(f"- Created By: {_user_label(row.owner)}")
	lines.append(f"- Created On: {frappe.utils.format_datetime(row.creation)}")
	if row.modified_by and row.modified_by != row.owner:
		lines.append(f"- Last Modified By: {_user_label(row.modified_by)} on {frappe.utils.format_datetime(row.modified)}")
	return "\n".join(lines)


def _run_missing_id(plan):
	"""Answer for an ID that matched no record the router could find."""
	where = plan["doctype"] or "any document type whose naming series matches it"
	return (
		f"No record with ID '{plan['doc_id']}' was found in {where}.\n"
		"Check the ID for typos (it must match exactly, including dashes)."
	)


def _run_user_info(plan):
	"""Answer for an email typed without any DocType to search."""
	email = plan["email"]
	user = frappe.db.get_value("User", {"name": email}, ["name", "full_name", "enabled", "user_type"], as_dict=True)
	if not user:
		return f"'{email}' is not a User in this system. Name a document type to search, e.g. 'latest purchase invoice by {email}'."
	if not frappe.has_permission("User", "read", doc=user.name) and user.name != frappe.session.user:
		return f"{email} is a User, but you do not have permission to see its details."
	return (
		f"User {user.name}: {user.full_name or '-'} ({'Enabled' if user.enabled else 'Disabled'}, {user.user_type}).\n"
		f"To see their documents, ask e.g. 'latest purchase invoice created by {email}'."
	)


def _run_one(plan):
	kind = plan.get("kind", "list")
	if kind == "document":
		return _run_document(plan)
	if kind == "missing_id":
		return _run_missing_id(plan)
	if kind == "user_info":
		return _run_user_info(plan)
	if plan["error"]:
		return plan["error"]
	if plan["is_count"]:
		return _count(plan)

	doctype = plan["doctype"]
	result = lookup_records(doctype=doctype, filters=plan["filters"], order_by=plan["order_by"], limit=plan["limit"])

	if result.get("error"):
		return f"I couldn't look up {doctype}: {result['error']}"

	rows = result.get("records") or []

	# A name may live in the ID or email rather than the title field (e.g. a
	# User whose full_name is blank) - retry on those before saying "none".
	if not rows and plan.get("name_words"):
		meta = frappe.get_meta(doctype)
		like = ["like", "%" + "%".join(plan["name_words"]) + "%"]
		for f in ("name", "email", "first_name", "full_name"):
			if f == plan["name_field"] or (f != "name" and not meta.has_field(f)):
				continue
			retry_filters = {**plan["filters"], f: like}
			retry_filters.pop(plan["name_field"], None)
			rows = (
				lookup_records(doctype=doctype, filters=retry_filters, order_by=plan["order_by"], limit=plan["limit"]).get(
					"records"
				)
				or []
			)
			if rows:
				break

	where = f" ({plan['label']})" if plan["label"] else ""
	if not rows:
		return (
			f"No {doctype} records found{where} that you have permission to see.\n"
			f"Check the {doctype} list view, or try a different date or name."
		)

	meta = frappe.get_meta(doctype)
	candidates = [
		meta.title_field, *plan["requested_fields"], plan.get("date_field"), plan.get("value_field"), "status", "grand_total"
	]
	if plan.get("show_owner"):
		candidates.insert(1, "owner")
	show = []
	for f in candidates:
		if f and f != "name" and f not in show and (meta.has_field(f) or f in ("creation", "owner")):
			show.append(f)
	show = show[:6]

	# lookup_records prunes its columns; refetch exactly the ones we want to
	# show (still via get_list, so permissions still apply).
	missing = [f for f in show if f not in rows[0]]
	if meta.has_field("currency") and "currency" not in rows[0]:
		missing.append("currency")
	if missing:
		extra = {
			r.name: r
			for r in frappe.get_list(
				doctype, filters={"name": ["in", [r["name"] for r in rows]]}, fields=["name", *missing]
			)
		}
		for r in rows:
			r.update({k: v for k, v in (extra.get(r["name"]) or {}).items() if k != "name"})
	show = [f for f in show if any(r.get(f) not in (None, "") for r in rows)]

	lines = [f"Found {len(rows)} {doctype} record{'s' if len(rows) != 1 else ''}{where}:"]
	for i, r in enumerate(rows, 1):
		parts = [str(r["name"])]
		for f in show:
			value = r.get(f)
			if value in (None, ""):
				continue
			if f == "owner":
				parts.append(f"Created By: {_user_label(value)}")
				continue
			df = meta.get_field(f)
			if df and df.fieldtype == "Currency":
				value = fmt_money(value, currency=r.get("currency"))
			parts.append(f"{meta.get_label(f) if df else frappe.unscrub(f)}: {value}")
		lines.append(f"{i}. " + " | ".join(parts))

	if len(rows) == plan["limit"]:
		which = {"creation asc": "oldest", "grand_total desc": "highest-value", "grand_total asc": "lowest-value"}
		lines.append(f"\nShowing the {which.get(plan['order_by'], 'latest')} {len(rows)}. Open the {doctype} list for more.")
	return "\n".join(lines)


def database_facts(message):
	"""Plain-text facts about any document ID or email typed in the message,
	looked up in the database. Handed to the LLM as grounding when the
	router itself doesn't answer, so the model never has to guess whether
	a record or user exists. Returns None when there is nothing to check."""
	emails, ids, _numbers, rest = _extract_entities(message)
	if not (emails or ids):
		return None
	targets = _detect_targets(rest, _tokens(rest))
	hint = targets[0][0] if len(targets) == 1 else None

	facts = []
	for doc_id in ids:
		found = find_document(doc_id, hint)
		plan = _document_plan(*found, []) if found else {"kind": "missing_id", "doc_id": doc_id, "doctype": hint}
		facts.append(_run_one(plan))
	for email in emails:
		facts.append(_run_user_info({"email": email}))
	return "\n\n".join(facts)
