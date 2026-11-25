# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Deterministic routing for data questions ("find/list/count/latest ...").

Small CPU-friendly local models (1.5B-3B) are unreliable at tool calling, so
plain data questions never depend on the model: this module turns the text
into lookup plans (DocType, filters, limit, ...), and answers.py runs them
through permission-checked get_list calls and formats the real rows.
Anything that doesn't look like a data question returns None and goes to
the LLM.

Order of checks: things typed verbatim (document IDs, emails) are looked up
in the database first, then ready-made questions (attention inbox, stock,
outstanding), then DocType + wording."""

import re

import frappe
from frappe.utils import cint

from support_ai.diagnostics import filters as wording
from support_ai.diagnostics.insights import (
	OUTSTANDING_WORDS,
	STOCK_WORDS,
	sum_field,
	wants_attention,
	wants_sum,
)
from support_ai.diagnostics.lookup import MAX_LIMIT
from support_ai.diagnostics.vocabulary import (
	DATA_INTENT_WORDS,
	DIAGNOSTIC_PHRASES,
	NEGATION_WORDS,
	STOP_WORDS,
	contains_phrase,
	detect_targets,
	extract_entities,
	find_document,
	has_fuzzy_intent,
	is_fuzzy,
	previous_doctype,
	tokens_of,
)

# Fields whose values the user names directly ("overdue invoices").
VALUE_FIELDS = ("status", "workflow_state")
DATE_FIELD_PREFERENCE = ("posting_date", "transaction_date", "date", "bill_date", "schedule_date")
# Intent words that can also legitimately name a column to show.
FIELD_HINT_WORDS = {"email", "emails", "mobile", "phone", "status", "total", "amount"}
NON_VALUE_FIELDTYPES = {
	"Section Break",
	"Column Break",
	"Tab Break",
	"Table",
	"Table MultiSelect",
	"HTML",
	"Button",
	"Fold",
}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10}
# "search code for X", "find in code: X", "where is this error coming from: X".
CODE_SEARCH_RE = re.compile(
	r"^\s*(?:please\s+)?(?:search|find|grep|look)\s+(?:in\s+|the\s+)?(?:custom\s+)?code\s*(?:for|:)?\s*(?P<query>.*)$"
	r"|^\s*code\s+search\s*[:\-]?\s*(?P<query2>.*)$"
	r"|^\s*(?:where|which\s+code)\s+(?:is|does)\s+(?:this\s+)?(?:error|message)\s+(?:coming\s+from|come\s+from|raised|thrown)\s*[:?\-]?\s*(?P<query3>.*)$",
	re.IGNORECASE | re.DOTALL,
)
# Requests to change data are never answered by the read-only router.
ACTION_WORDS = {
	"create",
	"make",
	"submit",
	"cancel",
	"delete",
	"update",
	"amend",
	"approve",
	"reject",
	"change",
	"modify",
}
# On a form, these ask about the open document itself.
REFERENCE_WORDS = {
	"items",
	"item",
	"taxes",
	"history",
	"approved",
	"approval",
	"approvals",
	"created",
	"owner",
	"details",
	"who",
}
LIST_CONTEXT_WORDS = {"these", "this list", "filtered", "here", "current list", "in this view"}


def plan_lookup(message, history=None, context=None, reference=None):
	"""Turns a message into lookup plans, or None when it isn't a plain data
	question (the caller then uses the LLM).

	Parameters:
		message (str, required): The user's message.
		history (list[str], optional): Earlier user messages, oldest first.
		context (dict, optional): {"doctype", "filters"} of the list view the
			chat was opened from.
		reference (tuple, optional): (doctype, name) of the form the chat was
			opened from - "who created this" / "show items" are about it.

	Returns:
		list[dict] | None: Plans for answers.run_planned_lookup.
	"""
	code_query = code_search_query(message)
	if code_query is not None:
		return [{"kind": "code_search", "query": code_query}]

	emails, ids, numbers, rest = extract_entities(message)
	tokens = tokens_of(rest)
	padded = " " + " ".join(tokens) + " "

	if any(f" {p} " in padded for p in DIAGNOSTIC_PHRASES) or set(tokens) & ACTION_WORDS:
		return None
	if not (emails or ids) and wants_attention(tokens):
		return [{"kind": "attention"}]
	targets = detect_targets(rest, tokens)
	if not (emails or ids or targets) and not _looks_like_data_question(tokens, context):
		return None

	if reference and not ids:
		return _reference_plans(reference, targets, tokens)
	special = _ready_made_plan(rest, tokens, targets, ids)
	if special:
		return [special]

	hint = targets[0][0] if len(targets) == 1 else None
	doc_plans = _document_plans(ids, numbers if hint else [], hint, tokens)
	if doc_plans:
		return doc_plans

	if emails:
		targets = _targets_for_email(targets, tokens, history)
		if not targets:
			return [{"kind": "user_info", "email": e} for e in emails]

	if len(targets) > 1:
		return [_clarify_plan(message, targets)]
	if not targets and context and context.get("doctype"):
		targets = [(context["doctype"], " ".join(tokens_of(context["doctype"])))]
	if not targets:
		return None

	list_filters = (context or {}).get("filters") if _refers_to_list(tokens, context, targets) else None
	return [
		_plan_for(dt, dt_norm, rest, tokens, emails=emails, list_filters=list_filters)
		for dt, dt_norm in targets
	]


def code_search_query(message):
	"""The text to search for when the message is a code-search command
	("search code for X", "where is this error coming from: X"), else None.

	Parameters:
		message (str, required): The user's message.

	Returns:
		str | None: Query text ("" when the command has no text after it).
	"""
	match = CODE_SEARCH_RE.match(message or "")
	if not match:
		return None
	text = next(g for g in match.group("query", "query2", "query3") if g is not None)
	return text.strip().strip("\"'“”").strip()


def _reference_plans(reference, targets, tokens):
	"""On a form: document-level questions are about that document; anything
	else that names no other DocType is a diagnosis for the LLM (None)."""
	other_targets = [t for t in targets if t[0] != reference[0]]
	if other_targets:
		return [_plan_for(dt, dt_norm, " ".join(tokens), tokens) for dt, dt_norm in other_targets[:1]]
	if set(tokens) & REFERENCE_WORDS:
		return [{"kind": "document", "doctype": reference[0], "name": reference[1], "tokens": tokens}]
	return None


def _looks_like_data_question(tokens, context):
	"""True when the wording asks for data (intent words, typos of them, or
	any question asked from a list view)."""
	if not tokens:
		return False
	return (
		bool(set(tokens) & DATA_INTENT_WORDS)
		or has_fuzzy_intent(tokens)
		or bool(context and context.get("doctype"))
	)


def _ready_made_plan(text, tokens, targets, ids):
	"""Stock balance / outstanding plans, or None."""
	if ids:
		return None
	target_names = {t[0] for t in targets}
	words = set(tokens)

	if words & STOCK_WORDS and ("item" in words or "of" in words) and not target_names - {"Item", "Bin"}:
		item_words = _words_after(tokens, ("item", "of", "for"))
		codes = wording.search_link("Item", item_words) if item_words else []
		return {"kind": "stock", "item_codes": codes, "words": item_words}

	invoice_targets = {"Purchase Invoice", "Sales Invoice"} & target_names
	if words & OUTSTANDING_WORDS and not (target_names - {"Supplier", "Customer"}) and not invoice_targets:
		party_type = "Customer" if words & {"customer", "customers", "receivable"} else "Supplier"
		party_words = _words_after(tokens, (party_type.lower(), party_type.lower() + "s"))
		names = wording.search_link(party_type, party_words) if party_words else None
		return {"kind": "outstanding", "party_type": party_type, "party_names": names, "words": party_words}
	return None


def _words_after(tokens, keys):
	"""Non-stop words following the last occurrence of any key word."""
	positions = [i for i, t in enumerate(tokens) if t in keys]
	if not positions:
		return []
	return wording.value_words(tokens, positions[-1] + 1, STOP_WORDS | {"stock", "balance", "item", "items"})


def _document_plans(ids, numbers, hint, tokens):
	"""Plans for exact document IDs typed in the message (found or missing)."""
	plans, missing = [], []
	for doc_id in ids + numbers:
		found = find_document(doc_id, hint)
		if found:
			plans.append({"kind": "document", "doctype": found[0], "name": found[1], "tokens": tokens})
		elif doc_id in ids:
			missing.append(doc_id)
	return plans + [{"kind": "missing_id", "doc_id": i, "doctype": hint} for i in missing]


def _targets_for_email(targets, tokens, history):
	"""Keeps the earlier DocType for a correction ("we don't have supplier
	x@y.com") or when the message names none."""
	ctx = previous_doctype(history)
	is_correction = bool(set(tokens) & NEGATION_WORDS)
	if ctx and (not targets or (is_correction and ctx[0] not in {t[0] for t in targets})):
		return [ctx]
	return targets


def _clarify_plan(message, targets):
	"""Asks which DocType a generic word ("invoice") meant, with one-click options."""
	alias = targets[0][1]
	options = []
	for doctype, _w in targets:
		prompt = re.sub(rf"\b{re.escape(alias)}\b", doctype, message, count=1, flags=re.IGNORECASE)
		options.append({"doctype": doctype, "prompt": prompt})
	return {"kind": "clarify", "word": alias, "options": options}


def _refers_to_list(tokens, context, targets):
	"""True when the user means the list view they are on ("these invoices")."""
	if not context or not context.get("filters") or not targets or targets[0][0] != context.get("doctype"):
		return False
	padded = " " + " ".join(tokens) + " "
	return any(f" {w} " in padded for w in LIST_CONTEXT_WORDS)


def _plan_for(doctype, dt_norm, text, tokens, emails=None, list_filters=None):
	"""Builds a list/count/sum plan for one DocType from the message wording.

	Parameters:
		doctype (str, required): DocType to list.
		dt_norm (str, required): The words that named it.
		text (str, required): Message text with entities removed.
		tokens (list[str], required): Its lowercase words.
		emails (list[str], optional): Emails typed in the message.
		list_filters (list, optional): Filters of the list view the user is on.

	Returns:
		dict: A "list" plan.
	"""
	plan = _PlanBuilder(doctype, dt_norm, text, tokens)
	plan.apply_list_filters(list_filters)
	plan.apply_emails(emails or [])
	plan.apply_value_fields()
	plan.apply_dates()
	plan.apply_amount()
	if not emails:
		plan.apply_links()
		plan.apply_items()
	plan.apply_requested_fields()
	plan.apply_name_search(skip=bool(emails))
	return plan.result()


class _PlanBuilder:
	"""Accumulates filters/labels for one DocType while marking the words
	each step used, so leftover words can become a name search."""

	def __init__(self, doctype, dt_norm, text, tokens):
		"""Starts an empty plan for doctype from the message words."""
		self.doctype, self.text, self.tokens = doctype, text, tokens
		self.meta = frappe.get_meta(doctype)
		dt_words = dt_norm.split()
		self.consumed = set(dt_words) | {w + "s" for w in dt_words} | {w + "es" for w in dt_words}
		# Typos of the DocType's own words ("inovoce") or of intent words ("lasted").
		self.consumed |= {
			t for t in tokens if is_fuzzy(t, dt_words) or is_fuzzy(t, ("latest", "recent"), 0.8, min_length=5)
		}
		self.filters, self.child_filters, self.labels, self.requested = {}, [], [], []
		self.error, self.date_field, self.value_field, self.name_field = None, None, None, None
		self.name_words = []
		self.show_owner = bool({"who", "owner", "created", "creator", "made"} & set(tokens))

	def apply_list_filters(self, list_filters):
		"""Adds the list view's own filters ([[dt, field, op, value], ...])."""
		for row in list_filters or []:
			if (
				len(row) >= 4
				and row[0] == self.doctype
				and (self.meta.has_field(row[1]) or row[1] in ("owner", "docstatus"))
			):
				self.filters[row[1]] = [row[2], row[3]]
		if list_filters:
			self.labels.append("filters of your list view")

	def apply_emails(self, emails):
		"""Email of a user -> Created By filter; other emails -> an email field."""
		for email in emails:
			user = frappe.db.get_value("User", email, ["name", "full_name"], as_dict=True)
			email_fields = [
				df.fieldname
				for df in self.meta.fields
				if df.fieldtype == "Data" and (df.options == "Email" or "email" in df.fieldname)
			]
			if user:
				self.filters["owner"] = user.name
				self.labels.append(f"Created By = {user.full_name or user.name} ({user.name})")
				self.show_owner = True
			elif email_fields:
				self.filters[email_fields[0]] = ["like", f"%{email}%"]
				self.labels.append(f"{self.meta.get_label(email_fields[0])} = {email}")
			else:
				self.error = f"'{email}' is not a User in this system, and {self.doctype} has no email field to search."

	def apply_value_fields(self):
		"""Status / workflow state named in the text; the longest match wins, so
		"partly paid" beats "paid"."""
		matches = []
		for fieldname in VALUE_FIELDS:
			matches += [
				(len(tokens_of(o)), fieldname, o)
				for o in _value_options(self.meta, self.doctype, fieldname)
				if contains_phrase(self.tokens, tokens_of(o))
			]
		if not matches:
			return
		_n, fieldname, option = max(matches, key=lambda m: m[0])
		self.filters[fieldname] = option
		self.value_field = fieldname
		self.consumed |= set(tokens_of(option)) | set(tokens_of(self.meta.get_label(fieldname)))
		self.labels.append(f"{self.meta.get_label(fieldname)} = {option}")

	def apply_dates(self):
		"""Date range on the DocType's main date field."""
		date_range, label, used = wording.detect_date_range(self.tokens, self.text, STOP_WORDS)
		self.consumed |= used
		if not date_range:
			return
		self.date_field = _date_field(self.meta)
		self.filters[self.date_field] = ["between", [str(date_range[0]), str(date_range[1])]]
		field_label = self.meta.get_label(self.date_field) if self.date_field != "creation" else "Created"
		self.labels.append(
			f"{field_label} {label if label.startswith(('since', 'up to', 'on')) else 'in ' + label}"
		)

	def apply_amount(self):
		"""'above 1 lakh' style amount condition."""
		found = wording.detect_amount_filter(self.meta, self.text)
		if found:
			field, condition, label, used = found
			self.filters[field] = condition
			self.labels.append(label)
			self.consumed |= used

	def apply_links(self):
		"""'for company jio' / 'of supplier goodedge' on Link fields."""
		skip = set(self.filters)
		filters, labels, used, errors = wording.detect_link_filters(self.meta, self.tokens, STOP_WORDS, skip)
		self.filters.update(filters)
		self.labels += labels
		self.consumed |= used
		if errors and not filters:
			self.error = errors[0]

	def apply_items(self):
		"""'containing item X' as a child-table filter."""
		found = wording.detect_item_filter(self.meta, self.tokens, STOP_WORDS)
		if found:
			self.child_filters, label, used = found
			self.labels.append(label)
			self.consumed |= used

	def apply_requested_fields(self):
		"""Words that name a column to show ("email", "supplier"). Per word,
		the exact fieldname match, else the shortest containing it."""
		words = [
			t
			for t in self.tokens
			if len(t) > 2 and t not in self.consumed and (t not in STOP_WORDS or t in FIELD_HINT_WORDS)
		]
		for t in words:
			best = _best_field_for(self.meta, t)
			if not best:
				continue
			if best not in self.requested:
				self.requested.append(best)
			self.consumed.add(t)

	def apply_name_search(self, skip=False):
		"""Leftover words become a name/title search - or an error when they
		were meant as a status the DocType doesn't have."""
		leftover = [
			t
			for t in self.tokens
			if t not in STOP_WORDS
			and t not in self.consumed
			and not t.isdigit()
			and wording.match_month(t, STOP_WORDS) is None
		]
		if skip or not leftover:
			return
		asked_for_status = bool({"status", "state", "pending"} & set(self.tokens))
		if asked_for_status and not self.value_field:
			self.error = self._unknown_status_message()
			return
		self.name_field = _name_search_field(self.meta, self.doctype)
		self.name_words = leftover
		self.filters[self.name_field] = ["like", "%" + "%".join(leftover) + "%"]
		field_label = self.meta.get_label(self.name_field) if self.name_field != "name" else "ID"
		self.labels.append(f"{field_label} like '{' '.join(leftover)}'")

	def _unknown_status_message(self):
		"""'X is not a status of DocType' plus the valid values."""
		wanted = " ".join(
			t for t in self.tokens if t not in self.consumed and t not in ("status", "state", "with", "is")
		)
		quoted = re.findall(r"[\"'“‘]([^\"'”’]+)[\"'”’]", self.text)
		lines = [f"'{(quoted or [wanted])[0].strip()}' is not a status of {self.doctype}."]
		for fieldname in VALUE_FIELDS:
			options = _value_options(self.meta, self.doctype, fieldname)
			if options:
				lines.append(f"Valid {self.meta.get_label(fieldname)} values: {', '.join(options)}")
		return "\n".join(lines)

	def result(self):
		"""The finished plan dict."""
		tokens, joined = self.tokens, " ".join(self.tokens)
		is_sum = wants_sum(tokens) and bool(sum_field(self.meta, tokens))
		return {
			"kind": "list",
			"doctype": self.doctype,
			"filters": self.filters,
			"child_filters": self.child_filters,
			"limit": _detect_limit(
				[t for t in tokens if t not in self.consumed or t in ("latest", "last", "top")]
			),
			"order_by": _order_by(self.meta, tokens),
			"is_count": not is_sum
			and ("count" in tokens or any(p in joined for p in ("how many", "number of"))),
			"is_sum": is_sum,
			"sum_field": sum_field(self.meta, tokens) if is_sum else None,
			"error": self.error,
			"requested_fields": self.requested[:4],
			"label": ", ".join(self.labels),
			"date_field": self.date_field,
			"value_field": self.value_field,
			"name_words": self.name_words,
			"name_field": self.name_field,
			"show_owner": self.show_owner,
		}


def _best_field_for(meta, word):
	"""Fieldname best matching a word: exact name, else fewest name parts."""
	candidates = [
		df
		for df in meta.fields
		if df.fieldtype not in NON_VALUE_FIELDTYPES
		and not df.hidden
		and (word in df.fieldname.split("_") or word in tokens_of(df.label or ""))
	]
	if not candidates:
		return None
	return min(candidates, key=lambda df: (df.fieldname != word, len(df.fieldname.split("_")))).fieldname


def _order_by(meta, tokens):
	"""Sort order from wording: oldest / highest / lowest, else newest first."""
	words = set(tokens)
	if {"oldest", "earliest"} & words:
		return "creation asc"
	if meta.has_field("grand_total") and {"highest", "largest", "biggest"} & words:
		return "grand_total desc"
	if meta.has_field("grand_total") and {"lowest", "smallest"} & words:
		return "grand_total asc"
	return "creation desc"


def _date_field(meta):
	"""The DocType's main date field, else creation."""
	return next((f for f in DATE_FIELD_PREFERENCE if meta.has_field(f)), "creation")


def _name_search_field(meta, doctype):
	"""Field to search a typed name in: title field, *_name, title or subject."""
	if meta.title_field and meta.has_field(meta.title_field):
		return meta.title_field
	return next(
		(f for f in ("full_name", frappe.scrub(doctype) + "_name", "title", "subject") if meta.has_field(f)),
		"name",
	)


def _detect_limit(tokens):
	"""How many rows to show: 'latest 3', 'show 2 ...', 'any' (1), 'all' (10), else 5."""
	for i, t in enumerate(tokens[:-1]):
		if t in ("latest", "lasted", "last", "top", "recent", "first"):
			nxt = tokens[i + 1]
			if nxt.isdigit():
				return min(cint(nxt), MAX_LIMIT)
			if nxt in NUMBER_WORDS:
				return NUMBER_WORDS[nxt]
	for t in tokens:
		# A bare count like "show me 2 draft invoices" (years excluded).
		if t.isdigit() and 0 < cint(t) <= 1000 and not re.fullmatch(r"(19|20)\d\d", t):
			return min(cint(t), MAX_LIMIT)
	if "any" in tokens or "anyone" in tokens:
		return 1
	if {"all", "list", "ids", "records"} & set(tokens):
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
			return list(
				dict.fromkeys(frappe.get_all("Workflow Document State", {"parent": workflow}, pluck="state"))
			)
	return []
