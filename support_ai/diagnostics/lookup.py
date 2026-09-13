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

EMPTY_VALUES = (None, "", 0, 0.0, False)


def _drop_uninformative_columns(rows, always_keep, display_cap):
	"""Doctypes like GL Entry carry several near-duplicate amount columns
	(debit/credit in account/transaction/reporting currency, exchange rates,
	...) that are almost always zero in a single-currency setup, crowding out
	fields with actual signal (e.g. voucher_type/voucher_no) within a fixed
	field budget. Fetching a generous superset of fields and pruning whatever
	turns out empty/zero across every returned row - here, after the query,
	rather than by hardcoding field names per doctype - keeps the field
	selection logic generic across arbitrary doctypes."""
	if not rows:
		return rows

	field_order = list(rows[0].keys())
	informative = [
		f for f in field_order if f in always_keep or any(row.get(f) not in EMPTY_VALUES for row in rows)
	]
	kept = (
		[f for f in field_order if f in always_keep]
		+ [f for f in informative if f not in always_keep][: max(display_cap - len(always_keep), 0)]
	)
	kept_set = set(kept)
	return [{k: v for k, v in row.items() if k in kept_set} for row in rows]

LOOKUP_TOOL_SCHEMA = {
	"type": "function",
	"function": {
		"name": "lookup_records",
		"description": (
			"List/search records of a Frappe DocType the user can read. Use ONLY for find/list/count/"
			"latest questions (e.g. 'latest journal entry'). For a ledger account's transactions, use "
			"doctype='GL Entry', filters={'account': '<exact account name>'} - NOT doctype='Journal Entry' "
			"(it has no direct account field). GL Entry rows include voucher_type/voucher_no naming the "
			"source document - use those, don't make a second unfiltered call to find it. "
			"Never guess filters for anything not explicitly asked."
		),
		"parameters": {
			"type": "object",
			"properties": {
				"doctype": {
					"type": "string",
					"description": "Exact real DocType name, e.g. 'GL Entry'. Never a record's own name/value.",
				},
				"filters": {
					"type": "object",
					"description": "Optional equality filters by fieldname, e.g. {'account': '...'}. Omit if not needed.",
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
		return {"error": f"{doctype!r} is not a valid DocType name. Pass a real Frappe DocType like 'GL Entry' or 'Journal Entry' - not a record name or value."}

	if not frappe.has_permission(doctype, "read"):
		return {"error": f"You do not have permission to read {doctype}."}

	try:
		limit = min(int(limit or DEFAULT_LIMIT), MAX_LIMIT)
	except (TypeError, ValueError):
		limit = DEFAULT_LIMIT

	# Small local models are inconsistent about argument typing - a "filters"
	# object frequently comes back stringified (e.g. '{}' or '{"foo": "bar"}')
	# instead of as an actual dict, so it can't be trusted as-is.
	filters_unparseable = False
	if isinstance(filters, str):
		raw = filters
		filters = None
		if raw.strip():
			try:
				filters = frappe.parse_json(raw)
			except Exception:
				try:
					# Small local models frequently emit Python dict repr
					# (single-quoted) instead of valid JSON, e.g. "{'a': 'b'}".
					import ast

					filters = ast.literal_eval(raw)
				except Exception:
					filters = None
		if filters is None:
			filters = {}
			if raw.strip():
				filters_unparseable = True
	if not isinstance(filters, dict):
		filters = {}

	meta = frappe.get_meta(doctype)
	standard_fields = {"name", "creation", "modified", "owner", "modified_by", "docstatus"}
	valid_fields = {df.fieldname for df in meta.fields} | standard_fields

	clean_filters = {}
	ignored_filters = {}
	for k, v in filters.items():
		if k in valid_fields:
			clean_filters[k] = v
		else:
			ignored_filters[k] = v

	order_by_parts = (order_by or "creation desc").lower().split()
	order_field = order_by_parts[0]
	if order_field not in valid_fields:
		order_field = "creation"
	order_direction = "asc" if "asc" in order_by_parts else "desc"

	# List-view fields alone are often not the analytically useful ones (e.g.
	# GL Entry's list view doesn't surface debit/credit/voucher_type/
	# voucher_no - exactly the fields needed to link a GL Entry back to its
	# originating Journal Entry/Sales Invoice/etc). So: always-applicable
	# fields first, then amount fields (almost always what "give me the data"
	# actually means), then in_list_view fields, then fill up to the cap with
	# any other visible scalar field in the doctype's own field order.
	FETCH_FIELDS_CAP = 30
	MAX_FIELDS = 15
	AMOUNT_FIELDTYPES = {"Currency", "Float", "Percent"}
	SCALAR_FIELDTYPES = AMOUNT_FIELDTYPES | {
		"Link",
		"Dynamic Link",
		"Int",
		"Date",
		"Datetime",
		"Time",
		"Data",
		"Select",
		"Check",
		"Small Text",
	}

	fields = ["name", "creation", "modified"]
	if meta.title_field and meta.title_field not in fields:
		fields.append(meta.title_field)
	for df in meta.fields:
		if len(fields) >= FETCH_FIELDS_CAP:
			break
		if df.fieldtype in AMOUNT_FIELDTYPES and not df.hidden and df.fieldname not in fields:
			fields.append(df.fieldname)
	for df in meta.fields:
		if len(fields) >= FETCH_FIELDS_CAP:
			break
		if df.in_list_view and df.fieldname not in fields:
			fields.append(df.fieldname)
	for df in meta.fields:
		if len(fields) >= FETCH_FIELDS_CAP:
			break
		if df.fieldtype in SCALAR_FIELDTYPES and not df.hidden and df.fieldname not in fields:
			fields.append(df.fieldname)
	fields = fields[:FETCH_FIELDS_CAP]

	rows = frappe.get_list(
		doctype,
		filters=clean_filters,
		fields=fields,
		order_by=f"{order_field} {order_direction}",
		limit_page_length=limit,
	)

	rows = _drop_uninformative_columns(rows, always_keep={"name", "creation", "modified"}, display_cap=MAX_FIELDS)

	result = {"doctype": doctype, "filters_applied": clean_filters, "count": len(rows), "records": rows}
	warnings = []
	if filters_unparseable:
		warnings.append(
			f"The filters argument could not be parsed and was IGNORED entirely - these {doctype} results "
			"are unfiltered. Do not present them as filtered; tell the user the filter failed and ask them "
			"to rephrase, e.g. naming the exact field and value."
		)
	if ignored_filters:
		warnings.append(
			f"These filter fields do not exist on {doctype} and were ignored (results are NOT filtered by "
			f"them): {ignored_filters}. Tell the user their {doctype} result may be broader than requested."
		)
	if warnings:
		result["warning"] = " ".join(warnings)
	return result
