# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Runs router plans and formats the real rows as markdown (clickable IDs,
tables, a Source line) plus one-click actions for the chat UI (open list
view, export, suggested follow-ups). Every read goes through get_list /
has_permission, and sensitive fields are masked."""

import frappe
from frappe.utils import fmt_money, format_datetime

from support_ai.diagnostics import insights
from support_ai.diagnostics.formatting import doc_link, markdown_table, source_line
from support_ai.diagnostics.lookup import lookup_records
from support_ai.diagnostics.masking import MASK, mask_record
from support_ai.diagnostics.vocabulary import (
	STOP_WORDS,
	detect_targets,
	extract_entities,
	find_document,
	tokens_of,
)

DOCSTATUS_LABELS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
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
# Fields worth showing for one document, in order, when present.
DOCUMENT_FIELDS = (
	"supplier",
	"supplier_name",
	"customer",
	"customer_name",
	"party_type",
	"party",
	"party_name",
	"employee",
	"employee_name",
	"company",
	"posting_date",
	"transaction_date",
	"bill_no",
	"bill_date",
	"due_date",
	"status",
	"workflow_state",
	"grand_total",
	"rounded_total",
	"outstanding_amount",
	"paid_amount",
	"total_debit",
	"voucher_type",
	"remarks",
)


def run_planned_lookup(plans):
	"""Runs each plan and merges the answers.

	Parameters:
		plans (list[dict], required): Output of router.plan_lookup.

	Returns:
		dict: {"text": markdown, "actions": [...], "no_result": bool}
	"""
	results = [_normalize(_run_one(plan)) for plan in plans]
	return {
		"text": "\n\n".join(r["text"] for r in results),
		"actions": [a for r in results for a in r["actions"]],
		"no_result": all(r["no_result"] for r in results),
	}


def database_facts(message):
	"""Plain-text facts about any document ID or email typed in the message,
	handed to the LLM as grounding so it never has to guess whether a record
	or user exists.

	Parameters:
		message (str, required): The user's message.

	Returns:
		str | None: Facts, or None when nothing was typed to check.
	"""
	emails, ids, _numbers, rest = extract_entities(message)
	if not (emails or ids):
		return None
	targets = detect_targets(rest, tokens_of(rest))
	hint = targets[0][0] if len(targets) == 1 else None

	facts = []
	for doc_id in ids:
		found = find_document(doc_id, hint)
		plan = (
			{"kind": "document", "doctype": found[0], "name": found[1], "tokens": []}
			if found
			else {"kind": "missing_id", "doc_id": doc_id, "doctype": hint}
		)
		facts.append(_normalize(_run_one(plan))["text"])
	facts += [_run_user_info({"email": e}) for e in emails]
	return "\n\n".join(facts)


def _normalize(result):
	"""Runner results may be plain text; make them the full dict shape."""
	if isinstance(result, str):
		return {"text": result, "actions": [], "no_result": False}
	result.setdefault("actions", [])
	result.setdefault("no_result", False)
	return result


def _run_one(plan):
	"""Dispatches one plan to its runner."""
	runners = {
		"document": _run_document,
		"missing_id": _run_missing_id,
		"user_info": _run_user_info,
		"clarify": _run_clarify,
		"attention": lambda _p: insights.attention_summary(),
		"stock": _run_stock,
		"outstanding": _run_outstanding,
	}
	runner = runners.get(plan.get("kind", "list"))
	if runner:
		return runner(plan)
	if plan["error"]:
		return {"text": plan["error"], "no_result": True}
	if plan["is_count"] or plan["is_sum"]:
		return _run_aggregate(plan)
	return _run_list(plan)


# --- Lists, counts and sums ----------------------------------------------------------


def _run_list(plan):
	"""Latest/oldest/top rows of a DocType as a table with list/export actions."""
	doctype = plan["doctype"]
	filters = _with_child_filters(plan)
	if filters is None:
		return _empty(plan)

	result = lookup_records(doctype=doctype, filters=filters, order_by=plan["order_by"], limit=plan["limit"])
	if result.get("error"):
		return {"text": f"I couldn't look up {doctype}: {result['error']}", "no_result": True}
	rows = result.get("records") or _retry_name_search(plan, filters)
	if not rows:
		return _empty(plan)

	meta = frappe.get_meta(doctype)
	show = _columns_to_show(meta, plan)
	rows = mask_record(_fill_columns(doctype, meta, rows, show))
	show = [f for f in show if any(r.get(f) not in (None, "") for r in rows)]

	header = ["ID"] + [_column_label(meta, f) for f in show]
	body = [[doc_link(doctype, r["name"])] + [_display(meta, f, r) for f in show] for r in rows]
	where = f" ({plan['label']})" if plan["label"] else ""
	lines = [
		f"**Found {len(rows)} {doctype} record{'s' if len(rows) != 1 else ''}**{where}",
		"",
		markdown_table(header, body),
	]
	if len(rows) == plan["limit"]:
		which = {
			"creation asc": "oldest",
			"grand_total desc": "highest-value",
			"grand_total asc": "lowest-value",
		}
		lines.append(f"\nShowing the {which.get(plan['order_by'], 'latest')} {len(rows)}.")
	lines.append(source_line(f"{doctype} list", plan["label"] or "no filters"))
	return {"text": "\n".join(lines), "actions": _list_actions(plan, filters, ["name", *show])}


def _run_aggregate(plan):
	"""Count or sum of matching records."""
	doctype = plan["doctype"]
	if not frappe.has_permission(doctype, "read"):
		return {"text": f"You do not have permission to read {doctype}.", "no_result": True}
	filters = _with_child_filters(plan)
	if filters is None:
		return _empty(plan)

	fields = [{"COUNT": "*", "as": "count"}]
	if plan["is_sum"]:
		fields.append({"SUM": plan["sum_field"], "as": "total"})
	row = frappe.get_list(doctype, filters=filters, fields=fields)[0]
	where = f" ({plan['label']})" if plan["label"] else ""
	count = row.get("count") or 0
	if plan["is_sum"]:
		label = frappe.get_meta(doctype).get_label(plan["sum_field"])
		text = f"**{doctype} — total {label}: {fmt_money(row.get('total') or 0)}** across {count:,} record{'s' if count != 1 else ''}{where}."
	else:
		text = f"**{doctype}: {count:,} record{'s' if count != 1 else ''}**{where}."
	text += source_line(f"{doctype} list", plan["label"] or "no filters")
	return {"text": text, "actions": _list_actions(plan, filters, ["name"]), "no_result": count == 0}


def _with_child_filters(plan):
	"""Plan filters, with any child-table filter ("containing item X")
	resolved to matching parent names. None means nothing can match."""
	if not plan.get("child_filters"):
		return plan["filters"]
	doctype = plan["doctype"]
	as_list = [[doctype, k, *(v if isinstance(v, list) else ["=", v])] for k, v in plan["filters"].items()]
	names = frappe.get_list(
		doctype,
		filters=as_list + plan["child_filters"],
		pluck="name",
		distinct=True,
		limit_page_length=500,
		order_by=plan["order_by"],
	)
	return {"name": ["in", list(dict.fromkeys(names))]} if names else None


def _retry_name_search(plan, filters):
	"""A name may live in the ID or email rather than the title field (e.g. a
	User whose full_name is blank) - retry on those before saying "none"."""
	if not plan.get("name_words"):
		return []
	meta = frappe.get_meta(plan["doctype"])
	like = ["like", "%" + "%".join(plan["name_words"]) + "%"]
	for field in ("name", "email", "first_name", "full_name"):
		if field == plan["name_field"] or (field != "name" and not meta.has_field(field)):
			continue
		retry = {**filters, field: like}
		retry.pop(plan["name_field"], None)
		rows = lookup_records(
			doctype=plan["doctype"], filters=retry, order_by=plan["order_by"], limit=plan["limit"]
		)
		if rows.get("records"):
			return rows["records"]
	return []


def _empty(plan):
	"""'No records found' answer."""
	where = f" ({plan['label']})" if plan["label"] else ""
	return {
		"text": f"No {plan['doctype']} records found{where} that you have permission to see.\n"
		f"Check the {plan['doctype']} list view, or try a different date or name.",
		"no_result": True,
		"actions": _list_actions(plan, plan["filters"], ["name"]),
	}


def _columns_to_show(meta, plan):
	"""Up to 6 columns: title, requested fields, owner, date, status, total."""
	candidates = [
		meta.title_field,
		*plan["requested_fields"],
		"owner" if plan.get("show_owner") else None,
		plan.get("date_field"),
		plan.get("value_field"),
		"status",
		"grand_total",
	]
	show = []
	for f in candidates:
		if f and f != "name" and f not in show and (meta.has_field(f) or f in ("creation", "owner")):
			show.append(f)
	return show[:6]


def _fill_columns(doctype, meta, rows, show):
	"""lookup_records prunes columns; refetch the ones we show (via get_list)."""
	missing = [f for f in show if f not in rows[0]]
	if meta.has_field("currency") and "currency" not in rows[0]:
		missing.append("currency")
	if not missing:
		return rows
	extra = {
		r.name: r
		for r in frappe.get_list(
			doctype, filters={"name": ["in", [r["name"] for r in rows]]}, fields=["name", *missing]
		)
	}
	for r in rows:
		r.update({k: v for k, v in (extra.get(r["name"]) or {}).items() if k != "name"})
	return rows


def _column_label(meta, fieldname):
	"""Column title for a fieldname."""
	if fieldname == "owner":
		return "Created By"
	return meta.get_label(fieldname) if meta.get_field(fieldname) else frappe.unscrub(fieldname)


def _display(meta, fieldname, row):
	"""Cell text: money in the row's currency, users by full name."""
	value = row.get(fieldname)
	if value in (None, ""):
		return ""
	if isinstance(value, str) and value.startswith(MASK):
		return value
	if fieldname in ("owner", "modified_by"):
		return frappe.db.get_value("User", value, "full_name") or value
	df = meta.get_field(fieldname)
	if df and df.fieldtype == "Currency":
		return fmt_money(value, currency=row.get("currency"))
	if df and df.fieldtype == "Link" and df.options:
		return doc_link(df.options, value)
	return str(value)[:80]


def _list_actions(plan, filters, fields):
	"""'Open in List View' and 'Export' buttons for the chat UI."""
	route_filters = {
		k: v for k, v in filters.items() if k != "name" or not isinstance(v, list) or len(v[1]) <= 50
	}
	return [
		{
			"type": "open_list",
			"label": f"Open {plan['doctype']} list",
			"doctype": plan["doctype"],
			"filters": route_filters,
		},
		{
			"type": "export",
			"label": "Export to CSV",
			"doctype": plan["doctype"],
			"filters": filters,
			"fields": fields,
		},
	]


# --- One document -------------------------------------------------------------------


def _run_document(plan):
	"""Details of one exact document the user named by ID - plus its child
	table rows or approval history when asked for."""
	doctype, name, tokens = plan["doctype"], plan["name"], plan.get("tokens") or []
	if not frappe.has_permission(doctype, "read", doc=name):
		return {
			"text": f"{doctype} {name} exists, but you do not have permission to read it.",
			"no_result": True,
		}

	if set(tokens) & insights.HISTORY_WORDS:
		return insights.workflow_history(doctype, name)
	child_rows = insights.child_table_rows(
		doctype, name, [t for t in tokens if t not in STOP_WORDS or t == "items"]
	)
	if child_rows:
		return f"{doc_link(doctype, name)}\n\n{child_rows}{source_line(doctype, name)}"
	return _document_details(doctype, name, tokens)


def _document_details(doctype, name, tokens):
	"""Key fields, status and who created/modified it."""
	meta = frappe.get_meta(doctype)
	fields = [
		f
		for f in (meta.title_field, *DOCUMENT_FIELDS, *_requested_fields(meta, tokens))
		if f and meta.has_field(f)
	]
	fields = list(dict.fromkeys(fields)) + (["currency"] if meta.has_field("currency") else [])
	rows = frappe.get_list(
		doctype,
		filters={"name": name},
		limit_page_length=1,
		fields=["name", "owner", "creation", "modified", "modified_by", "docstatus", *fields],
	)
	if not rows:
		return {
			"text": f"{doctype} {name} exists, but you do not have permission to read it.",
			"no_result": True,
		}
	row = mask_record(rows[0])

	lines = [f"**{doctype} {doc_link(doctype, name)}**", ""]
	for f in fields:
		df = meta.get_field(f)
		if (
			f == "currency"
			or row.get(f) in (None, "")
			or (df.fieldtype in ("Currency", "Float") and not row[f])
		):
			continue
		lines.append(f"- **{meta.get_label(f)}:** {_display(meta, f, row)}")
	if meta.is_submittable:
		lines.append(f"- **Document Status:** {DOCSTATUS_LABELS.get(row.docstatus, row.docstatus)}")
	lines.append(f"- **Created By:** {_user_label(row.owner)}")
	lines.append(f"- **Created On:** {format_datetime(row.creation)}")
	if row.modified_by and row.modified_by != row.owner:
		lines.append(
			f"- **Last Modified By:** {_user_label(row.modified_by)} on {format_datetime(row.modified)}"
		)
	lines.append(source_line(doctype, name))

	follow_ups = [
		{"type": "suggestion", "label": "Show items", "prompt": f"items in {name}"},
		{"type": "suggestion", "label": "Approval history", "prompt": f"approval history of {name}"},
	]
	return {"text": "\n".join(lines), "actions": follow_ups}


def _requested_fields(meta, tokens):
	"""Extra fields the user named, e.g. "bill no", "tax"."""
	requested = []
	for t in tokens:
		if len(t) <= 2 or t in STOP_WORDS:
			continue
		match = next(
			(
				df.fieldname
				for df in meta.fields
				if df.fieldtype not in NON_VALUE_FIELDTYPES and not df.hidden and t in df.fieldname.split("_")
			),
			None,
		)
		if match and match not in requested:
			requested.append(match)
	return requested


def _user_label(user):
	"""'Full Name (user@id)' for a User ID, or the ID if it has no name."""
	if not user:
		return ""
	full_name = frappe.db.get_value("User", user, "full_name")
	return f"{full_name} ({user})" if full_name and full_name != user else user


# --- Other answers ----------------------------------------------------------------------


def _run_missing_id(plan):
	"""Answer for an ID that matched no record."""
	where = plan["doctype"] or "any document type whose naming series matches it"
	return {
		"text": f"No record with ID '{plan['doc_id']}' was found in {where}.\n"
		"Check the ID for typos (it must match exactly, including dashes).",
		"no_result": True,
	}


def _run_user_info(plan):
	"""Answer for an email typed without any DocType to search."""
	email = plan["email"]
	user = frappe.db.get_value("User", email, ["name", "full_name", "enabled", "user_type"], as_dict=True)
	if not user:
		return f"'{email}' is not a User in this system. Name a document type to search, e.g. 'latest purchase invoice by {email}'."
	if user.name != frappe.session.user and not frappe.has_permission("User", "read", doc=user.name):
		return f"{email} is a User, but you do not have permission to see its details."
	return (
		f"User {user.name}: {user.full_name or '-'} ({'Enabled' if user.enabled else 'Disabled'}, {user.user_type}).\n"
		f"To see their documents, ask e.g. 'latest purchase invoice created by {email}'."
	)


def _run_clarify(plan):
	"""Asks which DocType a generic word meant, with one-click options."""
	names = " or ".join(o["doctype"] for o in plan["options"])
	return {
		"text": f"Did you mean **{names}**? Pick one below.",
		"actions": [
			{"type": "suggestion", "label": o["doctype"], "prompt": o["prompt"]} for o in plan["options"]
		],
	}


def _run_stock(plan):
	"""Stock balance of the named item(s)."""
	if not plan["item_codes"]:
		words = " ".join(plan.get("words") or [])
		return {
			"text": f"No Item matching '{words}' was found."
			if words
			else "Which item? e.g. 'stock of item ABC'.",
			"no_result": True,
		}
	return insights.stock_balance(plan["item_codes"])


def _run_outstanding(plan):
	"""Payable/receivable outstanding, optionally for named parties."""
	if plan["party_names"] == []:
		return {
			"text": f"No {plan['party_type']} matching '{' '.join(plan['words'])}' was found.",
			"no_result": True,
		}
	return insights.outstanding_summary(plan["party_type"], plan["party_names"])
