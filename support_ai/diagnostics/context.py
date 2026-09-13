# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Builds a structured, read-only diagnostic snapshot of a document: its
permission state, mandatory-field state, workflow state, related error logs,
and any custom code that could be influencing its behaviour. This is the
context handed to the LLM so it can explain *why* something is broken."""

import signal

import frappe

from support_ai.diagnostics.indexer import get_doctype_custom_code_context

VALIDATION_CHECK_TIMEOUT_SECONDS = 10


class _ValidationCheckTimeout(Exception):
	pass


def _alarm_handler(signum, frame):
	raise _ValidationCheckTimeout()

DOCSTATUS_LABELS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}

ALWAYS_INCLUDE_FIELDS = ("name", "owner", "modified", "modified_by", "docstatus")

SKIP_FIELDTYPES = {
	"Section Break",
	"Column Break",
	"Tab Break",
	"Fold",
	"Heading",
	"Button",
	"HTML",
	"Attach",
	"Attach Image",
	"Signature",
	"Password",
}


def build_document_context(doctype, name):
	if not frappe.db.exists(doctype, name):
		return {"error": f"{doctype} {name} does not exist."}

	if not frappe.has_permission(doctype, "read", doc=name):
		return {"error": f"You do not have permission to read {doctype} {name}."}

	doc = frappe.get_doc(doctype, name)
	meta = frappe.get_meta(doctype)

	context = {
		"_notes": (
			"field_values holds this specific document's actual current data. "
			"custom_code holds metadata about the doctype's structure and customizations "
			"(field definitions, scripts, hooks) - it is NOT this document's data."
		),
		"doctype": doctype,
		"name": name,
		"docstatus": doc.docstatus,
		"docstatus_label": DOCSTATUS_LABELS.get(doc.docstatus),
		"is_submittable": bool(meta.is_submittable),
		"permissions": {
			"read": frappe.has_permission(doctype, "read", doc=name),
			"write": frappe.has_permission(doctype, "write", doc=name),
			"submit": frappe.has_permission(doctype, "submit", doc=name),
			"cancel": frappe.has_permission(doctype, "cancel", doc=name),
		},
		"missing_mandatory_fields": get_missing_mandatory_fields(doc, meta),
		"validation_check": run_validation_check(doc),
		"workflow": get_workflow_context(doctype, doc),
		"recent_error_logs": get_related_error_logs(doctype, name),
		"custom_code": get_doctype_custom_code_context(doctype),
		"field_values": get_field_values(doc, meta),
	}
	return context


def get_field_values(doc, meta, max_fields=80):
	"""Actual current values on this document - not schema metadata. Skips
	layout-only fieldtypes and attachments/passwords, summarizes child tables
	by row count instead of dumping every row, and drops empty scalar fields
	to keep the LLM prompt focused and small (this matters a lot on
	CPU-only/low-RAM Ollama hosts where prompt size affects latency)."""
	values = {}
	child_tables = {}

	for df in meta.fields:
		if df.fieldtype in SKIP_FIELDTYPES:
			continue
		value = doc.get(df.fieldname)

		if df.fieldtype in ("Table", "Table MultiSelect"):
			if value:
				child_tables[df.fieldname] = f"{len(value)} row(s)"
			continue

		if value in (None, ""):
			continue

		values[df.fieldname] = value
		if len(values) >= max_fields:
			break

	for f in ALWAYS_INCLUDE_FIELDS:
		values.setdefault(f, doc.get(f))

	if child_tables:
		values["_child_tables"] = child_tables

	return values


def get_missing_mandatory_fields(doc, meta):
	missing = []
	for df in meta.fields:
		if df.reqd and not doc.get(df.fieldname):
			missing.append({"fieldname": df.fieldname, "label": df.label})
	return missing


def run_validation_check(doc):
	"""Re-runs validate() on an in-memory copy inside a rolled-back DB
	savepoint, purely to surface the real validation error message (if any).
	Never committed; always rolled back. Guarded by a hard wall-clock timeout
	because the savepoint can only undo database writes - it cannot undo an
	email send or an outbound webhook/API call a custom hook might make, so a
	hung external call is cut short rather than left to hang the request.
	Off by default; opt-in via Settings."""
	settings = frappe.get_cached_doc("Support AI Settings")
	if not settings.enable_live_validation_check:
		return "Live validation check is disabled in Support AI Settings."

	savepoint = "support_ai_validation_check"
	frappe.db.savepoint(savepoint)

	previous_handler = signal.signal(signal.SIGALRM, _alarm_handler)
	signal.alarm(VALIDATION_CHECK_TIMEOUT_SECONDS)
	try:
		test_doc = frappe.get_doc(doc.as_dict())
		test_doc.flags.ignore_permissions = True
		test_doc.flags.in_diagnostic_mode = True
		test_doc.run_method("validate")
		result = "No validation errors on the current data."
	except _ValidationCheckTimeout:
		result = (
			f"Validation check timed out after {VALIDATION_CHECK_TIMEOUT_SECONDS}s - a custom "
			"validate() hook is likely making a slow external call (e.g. a bank/GST integration). "
			"Skipped for safety."
		)
	except Exception as e:
		result = f"{e.__class__.__name__}: {e}"
	finally:
		signal.alarm(0)
		signal.signal(signal.SIGALRM, previous_handler)
		frappe.db.rollback(save_point=savepoint)

	return result


def get_workflow_context(doctype, doc):
	workflow_name = frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1})
	if not workflow_name:
		return {"has_workflow": False}

	workflow = frappe.get_doc("Workflow", workflow_name)
	state_field = workflow.workflow_state_field or "workflow_state"
	current_state = doc.get(state_field)
	user_roles = set(frappe.get_roles())

	allowed_transitions = []
	all_transitions = []
	for t in workflow.transitions:
		row = {
			"action": t.action,
			"from_state": t.state,
			"next_state": t.next_state,
			"allowed_role": t.allowed,
			"user_has_role": t.allowed in user_roles,
		}
		all_transitions.append(row)
		if t.state == current_state:
			allowed_transitions.append(row)

	return {
		"has_workflow": True,
		"workflow_name": workflow_name,
		"state_field": state_field,
		"current_state": current_state,
		"transitions_from_current_state": allowed_transitions,
		"all_transitions": all_transitions,
		"user_roles": sorted(user_roles),
	}


def get_related_error_logs(doctype, name, limit=5, max_chars=2000):
	"""Looks up Error Log by its indexed reference_doctype/reference_name
	columns - never a free-text LIKE scan, which on a large Error Log table
	(this site has millions of rows) would be an unindexed full table scan
	and can take minutes."""
	logs = frappe.get_all(
		"Error Log",
		filters={"reference_doctype": doctype, "reference_name": name},
		fields=["name", "error", "creation"],
		order_by="creation desc",
		limit_page_length=limit,
	)
	for log in logs:
		if log.error and len(log.error) > max_chars:
			log.error = log.error[:max_chars] + "... (truncated)"
	return logs
