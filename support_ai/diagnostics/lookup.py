# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""A single, narrow read-only tool the LLM can call for general "find/list/
latest" style questions that aren't about one already-referenced document.
Always goes through frappe.get_list(), so the caller's own role permissions,
user permissions, and permission query conditions apply exactly as they
would in a List View - this can never surface more than the asking user
could already see themselves. Filtering/ordering is restricted to the
doctype's real fieldnames (no raw SQL), and results are hard-capped."""

import frappe

MAX_LIMIT = 20
DEFAULT_LIMIT = 5

LOOKUP_TOOL_SCHEMA = {
	"type": "function",
	"function": {
		"name": "lookup_records",
		"description": (
			"List or search records of a Frappe DocType that the user has permission to read. "
			"Use this ONLY when the user asks to find, list, count, or get the latest/recent record(s) "
			"of some DocType (e.g. 'latest journal entry', 'list open sales invoices for customer X'). "
			"Do not use it, and do not guess filters, for anything not explicitly asked."
		),
		"parameters": {
			"type": "object",
			"properties": {
				"doctype": {
					"type": "string",
					"description": "Exact Frappe DocType name, e.g. 'Journal Entry', 'Sales Invoice'.",
				},
				"filters": {
					"type": "object",
					"description": 'Optional simple equality filters, e.g. {"customer": "..."}. Omit if not needed.',
				},
				"order_by": {
					"type": "string",
					"description": "Fieldname to sort by, e.g. 'creation'. Latest-first is the default.",
				},
				"limit": {
					"type": "integer",
					"description": f"Max records to return (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
				},
			},
			"required": ["doctype"],
		},
	},
}


def lookup_records(doctype=None, filters=None, order_by=None, limit=None):
	if not doctype or not frappe.db.exists("DocType", doctype):
		return {"error": f"{doctype!r} is not a valid DocType."}

	if not frappe.has_permission(doctype, "read"):
		return {"error": f"You do not have permission to read {doctype}."}

	try:
		limit = min(int(limit or DEFAULT_LIMIT), MAX_LIMIT)
	except (TypeError, ValueError):
		limit = DEFAULT_LIMIT

	# Small local models are inconsistent about argument typing - a "filters"
	# object frequently comes back stringified (e.g. '{}' or '{"foo": "bar"}')
	# instead of as an actual dict, so it can't be trusted as-is.
	if isinstance(filters, str):
		try:
			filters = frappe.parse_json(filters) if filters.strip() else {}
		except Exception:
			filters = {}
	if not isinstance(filters, dict):
		filters = {}

	meta = frappe.get_meta(doctype)
	standard_fields = {"name", "creation", "modified", "owner", "modified_by", "docstatus"}
	valid_fields = {df.fieldname for df in meta.fields} | standard_fields

	clean_filters = {}
	for k, v in filters.items():
		if k in valid_fields:
			clean_filters[k] = v

	order_by_parts = (order_by or "creation desc").lower().split()
	order_field = order_by_parts[0]
	if order_field not in valid_fields:
		order_field = "creation"
	order_direction = "asc" if "asc" in order_by_parts else "desc"

	fields = ["name", "creation", "modified"]
	if meta.title_field and meta.title_field not in fields:
		fields.append(meta.title_field)
	for df in meta.fields:
		if df.in_list_view and df.fieldname not in fields:
			fields.append(df.fieldname)
	fields = fields[:10]

	rows = frappe.get_list(
		doctype,
		filters=clean_filters,
		fields=fields,
		order_by=f"{order_field} {order_direction}",
		limit_page_length=limit,
	)

	return {"doctype": doctype, "filters_applied": clean_filters, "count": len(rows), "records": rows}
