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

SNAPSHOT_FIELDS = ("name", "owner", "modified", "modified_by", "status", "workflow_state", "docstatus")


def build_document_context(doctype, name):
	if not frappe.db.exists(doctype, name):
		return {"error": f"{doctype} {name} does not exist."}

	if not frappe.has_permission(doctype, "read", doc=name):
		return {"error": f"You do not have permission to read {doctype} {name}."}

	doc = frappe.get_doc(doctype, name)
	meta = frappe.get_meta(doctype)

	context = {
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
		"recent_error_logs": get_related_error_logs(name),
		"custom_code": get_doctype_custom_code_context(doctype),
		"field_snapshot": {f: doc.get(f) for f in SNAPSHOT_FIELDS if doc.get(f) is not None},
	}
	return context


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


def get_related_error_logs(name, limit=5, max_chars=2000):
	logs = frappe.get_all(
		"Error Log",
		filters=[["error", "like", f"%{name}%"]],
		fields=["name", "error", "creation"],
		order_by="creation desc",
		limit_page_length=limit,
	)
	for log in logs:
		if log.error and len(log.error) > max_chars:
			log.error = log.error[:max_chars] + "... (truncated)"
	return logs
