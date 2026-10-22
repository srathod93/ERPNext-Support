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
		if t in ("latest", "last", "top", "recent", "first") and i + 1 < len(tokens):
			nxt = tokens[i + 1]
			if nxt.isdigit():
				return min(cint(nxt), MAX_LIMIT)
			if nxt in NUMBER_WORDS:
				return NUMBER_WORDS[nxt]
	for t in tokens:
		# A bare count like "show me 2 draft invoices" (years excluded).
		if t.isdigit() and 0 < cint(t) <= 1000 and not re.fullmatch(r"(19|20)\d\d", t):
			return min(cint(t), MAX_LIMIT)
	if "any one" in " ".join(tokens) or "anyone" in tokens:
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


def plan_lookup(message):
	"""Returns a list of lookup plans (one per DocType), or None if this
	doesn't look like a plain data question - the caller then uses the LLM."""
	tokens = _tokens(message)
	if not tokens or not (set(tokens) & DATA_INTENT_WORDS):
		return None
	padded = " " + " ".join(tokens) + " "
	if any(f" {p} " in padded for p in DIAGNOSTIC_PHRASES):
		return None

	targets = _detect_targets(message, tokens)
	if not targets:
		return None
	return [_plan_for(doctype, dt_norm, message, tokens) for doctype, dt_norm in targets]


def _plan_for(doctype, dt_norm, message, tokens):
	meta = frappe.get_meta(doctype)
	joined = " ".join(tokens)

	consumed = set(dt_norm.split()) | {w + "s" for w in dt_norm.split()} | {w + "es" for w in dt_norm.split()}

	filters = {}
	labels = []
	error = None

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


def _run_one(plan):
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
	show = []
	for f in candidates:
		if f and f != "name" and f not in show and (meta.has_field(f) or f == "creation"):
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
			df = meta.get_field(f)
			if df and df.fieldtype == "Currency":
				value = fmt_money(value, currency=r.get("currency"))
			parts.append(f"{meta.get_label(f) if df else frappe.unscrub(f)}: {value}")
		lines.append(f"{i}. " + " | ".join(parts))

	if len(rows) == plan["limit"]:
		which = {"creation asc": "oldest", "grand_total desc": "highest-value", "grand_total asc": "lowest-value"}
		lines.append(f"\nShowing the {which.get(plan['order_by'], 'latest')} {len(rows)}. Open the {doctype} list for more.")
	return "\n".join(lines)
