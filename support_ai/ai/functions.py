# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Handlers for every Support AI Function type, plus the JSON-schema each
one exposes to the model.

Every handler runs as the asking user through permission-checked Frappe APIs
(get_list, client.get, insert/save without ignore_permissions, ...), so the
AI can never read or change more than the user could themselves in the UI.
Write functions are additionally gated by the Support AI write role - see
check_function_access()."""

import json

import frappe
from frappe import _

from support_ai.diagnostics.masking import mask_record

READ_TYPES = (
	"Get Document",
	"Get Multiple Documents",
	"Get List",
	"Get Value",
	"Get Amended Document",
	"Get Report Result",
)
WRITE_TYPES = (
	"Create Document",
	"Create Multiple Documents",
	"Update Document",
	"Update Multiple Documents",
	"Delete Document",
	"Delete Multiple Documents",
	"Submit Document",
	"Cancel Document",
	"Set Value",
	"Attach File to Document",
)
NO_DOCTYPE_TYPES = ("Custom Function", "Get Report Result", "Attach File to Document")

DEFAULT_WRITE_ROLE = "Support AI Action User"
MAX_LIST_LIMIT = 50
MAX_REPORT_ROWS = 50

# Fields the AI may never set directly on create/update.
PROTECTED_FIELDS = {
	"doctype",
	"name",
	"owner",
	"creation",
	"modified",
	"modified_by",
	"docstatus",
	"idx",
	"amended_from",
}


# --- Access ---------------------------------------------------------------------


def get_write_role():
	settings = frappe.get_cached_doc("Support AI Settings")
	return settings.get("write_action_role") or DEFAULT_WRITE_ROLE


def check_function_access(function_doc, bot=None):
	"""Returns (allowed, reason). Write functions need, all at once: a bot
	with Allow Write Actions on, and the Support AI write role on the user.
	Any function can also be limited to specific roles. Normal DocType
	permissions are enforced again when the handler runs."""
	user_roles = set(frappe.get_roles())

	if function_doc.allowed_roles:
		allowed = {r.role for r in function_doc.allowed_roles}
		if not (allowed & user_roles):
			return False, _("Function {0} is limited to roles: {1}").format(
				function_doc.name, ", ".join(sorted(allowed))
			)

	if function_doc.requires_write_permissions:
		if bot is not None and not bot.allow_write_actions:
			return False, _("Bot {0} is not allowed to perform write actions.").format(bot.name)
		write_role = get_write_role()
		if write_role not in user_roles:
			return False, _("You need the role '{0}' to let Support AI change data.").format(write_role)

	return True, None


# --- JSON schema ------------------------------------------------------------------


def _param_property(param):
	prop = {"type": param.type or "string"}
	if param.description:
		prop["description"] = param.description
	if param.options:
		options = [o.strip() for o in param.options.split("\n") if o.strip()]
		if options:
			prop["enum"] = options
	if prop["type"] == "array":
		prop["items"] = {"type": "string"}
	return prop


def _params_object(function_doc, description=None):
	properties, required = {}, []
	for param in function_doc.params or []:
		if param.do_not_ask_ai:
			continue
		properties[param.fieldname] = _param_property(param)
		if param.required:
			required.append(param.fieldname)
	schema = {"type": "object", "properties": properties}
	if description:
		schema["description"] = description
	if required:
		schema["required"] = required
	if function_doc.strict:
		schema["additionalProperties"] = False
	return schema


def _object(properties, required=None, strict=False):
	schema = {"type": "object", "properties": properties}
	if required:
		schema["required"] = required
	if strict:
		schema["additionalProperties"] = False
	return schema


def build_parameters_schema(function_doc):
	t = function_doc.type
	dt = function_doc.reference_doctype or "document"
	has_params = bool([p for p in function_doc.params or [] if not p.do_not_ask_ai])
	strict = bool(function_doc.strict)

	doc_id = {"type": "string", "description": f"The ID (name) of the {dt}"}
	doc_ids = {"type": "array", "items": {"type": "string"}, "description": f"IDs of the {dt} records"}
	any_object = {"type": "object", "description": f"Field values for the {dt}, keyed by fieldname"}

	if t in ("Get Document", "Delete Document", "Submit Document", "Cancel Document", "Get Amended Document"):
		return _object({"document_id": doc_id}, ["document_id"], strict)

	if t in ("Get Multiple Documents", "Delete Multiple Documents"):
		return _object({"document_ids": doc_ids}, ["document_ids"], strict)

	if t == "Get List":
		filters = (
			_params_object(function_doc, "Filters, keyed by fieldname")
			if has_params
			else {"type": "object", "description": f"Optional equality filters on {dt}, keyed by fieldname"}
		)
		return _object(
			{
				"filters": filters,
				"fields": {"type": "array", "items": {"type": "string"}, "description": "Fieldnames to return"},
				"order_by": {"type": "string", "description": "e.g. 'creation desc'"},
				"limit": {"type": "integer", "description": f"Max records (default 20, max {MAX_LIST_LIMIT})"},
			},
			strict=strict,
		)

	if t == "Get Value":
		return _object(
			{
				"filters": {"type": "object", "description": f"Filters that identify the {dt}, or use document_id"},
				"document_id": doc_id,
				"fieldname": {"type": "string", "description": "Fieldname to read (comma-separate several)"},
			},
			["fieldname"],
			strict,
		)

	if t == "Create Document":
		return _params_object(function_doc) if has_params else _object({"data": any_object}, ["data"], strict)

	if t == "Create Multiple Documents":
		item = _params_object(function_doc) if has_params else any_object
		return _object({"documents": {"type": "array", "items": item}}, ["documents"], strict)

	if t == "Update Document":
		if has_params:
			schema = _params_object(function_doc)
			schema["properties"] = {"document_id": doc_id, **schema["properties"]}
			schema["required"] = ["document_id"]
			return schema
		return _object({"document_id": doc_id, "data": any_object}, ["document_id", "data"], strict)

	if t == "Update Multiple Documents":
		if has_params:
			item = _params_object(function_doc)
			item["properties"] = {"document_id": doc_id, **item["properties"]}
			item["required"] = ["document_id"]
		else:
			item = _object({"document_id": doc_id, "data": any_object}, ["document_id", "data"])
		return _object({"documents": {"type": "array", "items": item}}, ["documents"], strict)

	if t == "Set Value":
		return _object(
			{
				"document_id": doc_id,
				"fieldname": {"type": "string", "description": "Fieldname to set"},
				"value": {"type": "string", "description": "New value"},
			},
			["document_id", "fieldname", "value"],
			strict,
		)

	if t == "Attach File to Document":
		properties = {
			"document_id": doc_id,
			"file_url": {"type": "string", "description": "file_url of an existing file, e.g. one the user attached"},
		}
		required = ["document_id", "file_url"]
		if not function_doc.reference_doctype:
			properties["doctype"] = {"type": "string", "description": "DocType of the document"}
			required.append("doctype")
		return _object(properties, required, strict)

	if t == "Get Report Result":
		filters = (
			_params_object(function_doc, "Report filters")
			if has_params
			else {"type": "object", "description": "Report filters, keyed by filter fieldname"}
		)
		return _object({"filters": filters}, strict=strict)

	if t == "Custom Function":
		return _params_object(function_doc)

	frappe.throw(_("Unsupported function type: {0}").format(t))


# --- Argument helpers -------------------------------------------------------------


def _parse(value, default=None):
	if isinstance(value, str):
		value = value.strip()
		if not value:
			return default
		try:
			return json.loads(value)
		except ValueError:
			return default if default is not None else value
	return default if value is None else value


def _cast(value, type_):
	if value is None:
		return None
	try:
		if type_ == "integer":
			return int(value)
		if type_ == "number":
			return float(value)
		if type_ == "boolean":
			return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes")
		if type_ in ("array", "object"):
			return _parse(value)
	except (TypeError, ValueError):
		return value
	return value


def _param_values(function_doc, args):
	"""Declared params only, with defaults (and always-default 'do not ask AI'
	params) applied."""
	values = {}
	for param in function_doc.params or []:
		if param.do_not_ask_ai:
			value = param.default_value
		else:
			value = args.get(param.fieldname)
			if value in (None, "") and param.default_value not in (None, ""):
				value = param.default_value
		if value not in (None, ""):
			values[param.fieldname] = _cast(value, param.type)
	return values


def _document_data(function_doc, args):
	if [p for p in function_doc.params or []]:
		data = _param_values(function_doc, args)
	else:
		data = _parse(args.get("data"), {}) or {}
	if not isinstance(data, dict):
		frappe.throw(_("'data' must be an object of fieldname: value."))
	return {k: v for k, v in data.items() if k not in PROTECTED_FIELDS}


def _require(args, key):
	value = args.get(key)
	if value in (None, "", []):
		frappe.throw(_("Missing required argument: {0}").format(key))
	return value


def _compact(d):
	"""Drops empty values and internal fields so a document fits in context."""
	skip = {"doctype", "owner", "modified_by", "idx", "parent", "parentfield", "parenttype", "lft", "rgt"}
	out = {}
	for k, v in d.items():
		if k in skip or k.startswith("_") or v is None or v == "" or v == []:
			continue
		if isinstance(v, list):
			v = [_compact(row) if isinstance(row, dict) else row for row in v]
		out[k] = v
	return out


# --- Handlers ---------------------------------------------------------------------


def get_document(function_doc, args):
	from frappe.client import get

	return mask_record(_compact(get(function_doc.reference_doctype, _require(args, "document_id"))))


def get_multiple_documents(function_doc, args):
	ids = _parse(_require(args, "document_ids"), [])
	return [get_document(function_doc, {"document_id": i}) for i in ids[:MAX_LIST_LIMIT]]


def get_list(function_doc, args):
	dt = function_doc.reference_doctype
	meta = frappe.get_meta(dt)
	valid = {df.fieldname for df in meta.fields} | {"name", "creation", "modified", "owner", "docstatus"}

	filters = _param_values(function_doc, _parse(args.get("filters"), {}) or {}) if function_doc.params else (
		_parse(args.get("filters"), {}) or {}
	)
	if isinstance(filters, dict):
		bad = [k for k in filters if k not in valid]
		if bad:
			return {"error": f"Unknown filter fields on {dt}: {bad}. Valid fields include: {sorted(valid)[:40]}"}

	fields = _parse(args.get("fields"), []) or []
	if isinstance(fields, str):
		fields = [f.strip() for f in fields.split(",")]
	fields = [f for f in fields if f in valid] or (
		["name"]
		+ ([meta.title_field] if meta.title_field else [])
		+ [df.fieldname for df in meta.fields if df.in_list_view][:8]
	)

	order_by = args.get("order_by") or "creation desc"
	order_parts = str(order_by).split()
	if order_parts[0] not in valid:
		order_by = "creation desc"
	else:
		order_by = f"{order_parts[0]} {'asc' if 'asc' in order_parts[1:] else 'desc'}"

	limit = min(int(args.get("limit") or 20), MAX_LIST_LIMIT)
	rows = frappe.get_list(dt, filters=filters, fields=list(dict.fromkeys(fields)), order_by=order_by, limit_page_length=limit)
	return {"doctype": dt, "count": len(rows), "records": mask_record(rows)}


def get_value(function_doc, args):
	from frappe.client import get_value as client_get_value

	fieldname = _require(args, "fieldname")
	if isinstance(fieldname, str) and "," in fieldname:
		fieldname = [f.strip() for f in fieldname.split(",")]
	filters = args.get("document_id") or _parse(args.get("filters"), {})
	if not filters:
		frappe.throw(_("Pass document_id or filters."))
	return client_get_value(function_doc.reference_doctype, fieldname, filters)


def get_amended_document(function_doc, args):
	dt = function_doc.reference_doctype
	amended = frappe.get_list(dt, filters={"amended_from": _require(args, "document_id")}, pluck="name", limit=1)
	if not amended:
		return {"message": f"{dt} {args.get('document_id')} has not been amended."}
	return get_document(function_doc, {"document_id": amended[0]})


def create_document(function_doc, args):
	data = _document_data(function_doc, args)
	doc = frappe.get_doc({**data, "doctype": function_doc.reference_doctype})
	doc.insert()
	return {"message": f"Created {doc.doctype} {doc.name}", "doctype": doc.doctype, "document_id": doc.name}


def create_multiple_documents(function_doc, args):
	documents = _parse(_require(args, "documents"), [])
	return [create_document(function_doc, item if function_doc.params else {"data": item}) for item in documents]


def update_document(function_doc, args):
	dt = function_doc.reference_doctype
	doc = frappe.get_doc(dt, _require(args, "document_id"))
	doc.check_permission("write")
	data = _document_data(function_doc, args)
	if not data:
		frappe.throw(_("Nothing to update."))
	doc.update(data)
	doc.save()
	return {"message": f"Updated {dt} {doc.name}", "doctype": dt, "document_id": doc.name, "updated_fields": list(data)}


def update_multiple_documents(function_doc, args):
	documents = _parse(_require(args, "documents"), [])
	return [update_document(function_doc, item) for item in documents]


def delete_document(function_doc, args):
	dt = function_doc.reference_doctype
	name = _require(args, "document_id")
	frappe.delete_doc(dt, name)
	return {"message": f"Deleted {dt} {name}", "doctype": dt, "document_id": name}


def delete_multiple_documents(function_doc, args):
	ids = _parse(_require(args, "document_ids"), [])
	return [delete_document(function_doc, {"document_id": i}) for i in ids]


def submit_document(function_doc, args):
	dt = function_doc.reference_doctype
	doc = frappe.get_doc(dt, _require(args, "document_id"))
	doc.check_permission("submit")
	doc.submit()
	return {"message": f"Submitted {dt} {doc.name}", "doctype": dt, "document_id": doc.name}


def cancel_document(function_doc, args):
	dt = function_doc.reference_doctype
	doc = frappe.get_doc(dt, _require(args, "document_id"))
	doc.check_permission("cancel")
	doc.cancel()
	return {"message": f"Cancelled {dt} {doc.name}", "doctype": dt, "document_id": doc.name}


def set_value(function_doc, args):
	from frappe.client import set_value as client_set_value

	dt = function_doc.reference_doctype
	fieldname = _require(args, "fieldname")
	if fieldname in PROTECTED_FIELDS:
		frappe.throw(_("Field {0} cannot be set by Support AI.").format(fieldname))
	client_set_value(dt, _require(args, "document_id"), fieldname, args.get("value"))
	return {"message": f"Set {fieldname} on {dt} {args['document_id']}", "doctype": dt, "document_id": args["document_id"]}


def attach_file_to_document(function_doc, args):
	dt = function_doc.reference_doctype or _require(args, "doctype")
	doc = frappe.get_doc(dt, _require(args, "document_id"))
	doc.check_permission("write")

	source = frappe.get_doc("File", {"file_url": _require(args, "file_url")})
	source.check_permission("read")

	attached = frappe.get_doc(
		{
			"doctype": "File",
			"file_url": source.file_url,
			"file_name": source.file_name,
			"is_private": source.is_private,
			"attached_to_doctype": dt,
			"attached_to_name": doc.name,
		}
	).insert()
	return {"message": f"Attached {source.file_name} to {dt} {doc.name}", "file": attached.name, "document_id": doc.name}


def get_report_result(function_doc, args):
	from frappe.desk.query_report import run

	filters = _parse(args.get("filters"), {}) or {}
	if function_doc.params:
		filters = _param_values(function_doc, filters)

	# run() checks the user's access to the report and its reference DocType.
	result = run(function_doc.report_name, filters=filters, ignore_prepared_report=True)

	columns = []
	for column in result.get("columns") or []:
		if isinstance(column, dict):
			columns.append(column.get("fieldname") or column.get("label"))
		else:
			columns.append(str(column).split(":")[0])

	rows = result.get("result") or []
	total = len(rows)
	trimmed = []
	for row in rows[:MAX_REPORT_ROWS]:
		if isinstance(row, (list, tuple)):
			row = dict(zip(columns, row))
		if isinstance(row, dict):
			row = {k: v for k, v in row.items() if v not in (None, "")}
		trimmed.append(row)

	out = {"report": function_doc.report_name, "filters": filters, "columns": columns, "total_rows": total, "rows": trimmed}
	if total > MAX_REPORT_ROWS:
		out["note"] = f"Only the first {MAX_REPORT_ROWS} of {total} rows are included."
	return out


def custom_function(function_doc, args):
	fn = frappe.get_attr(function_doc.function_path)
	# Only functions already exposed to users through @frappe.whitelist() can be
	# called, so a custom function can never do more than the user could by
	# calling the same API themselves.
	frappe.is_whitelisted(fn)

	values = _param_values(function_doc, args) if function_doc.params else dict(args)
	if function_doc.pass_parameters_as_json:
		return fn(json.dumps(values, default=str))
	return fn(**values)


HANDLERS = {
	"Get Document": get_document,
	"Get Multiple Documents": get_multiple_documents,
	"Get List": get_list,
	"Get Value": get_value,
	"Get Amended Document": get_amended_document,
	"Create Document": create_document,
	"Create Multiple Documents": create_multiple_documents,
	"Update Document": update_document,
	"Update Multiple Documents": update_multiple_documents,
	"Delete Document": delete_document,
	"Delete Multiple Documents": delete_multiple_documents,
	"Submit Document": submit_document,
	"Cancel Document": cancel_document,
	"Set Value": set_value,
	"Attach File to Document": attach_file_to_document,
	"Get Report Result": get_report_result,
	"Custom Function": custom_function,
}


def run_function(function_doc, args):
	handler = HANDLERS.get(function_doc.type)
	if not handler:
		frappe.throw(_("Unsupported function type: {0}").format(function_doc.type))
	return handler(function_doc, args or {})
