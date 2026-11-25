# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Everything on this site that changes how one DocType behaves, with the
real source code of each hook function - not just its dotted path:

- hooks.py of custom apps: doc_events (incl. "*"), override_doctype_class,
  doctype_js / doctype_list_js, permission_query_conditions, has_permission,
  and override_whitelisted_methods that belong to the DocType
- Server Scripts, Client Scripts, Custom Fields, Property Setters
- active Workflow, Notifications, Webhooks, Assignment Rules, User Permission
  counts and custom Reports

Sections are filled in priority order until a character budget is spent,
so a small model's prompt never overflows. Code sections need the code
access role; without it only the names are listed."""

import inspect
import json
import os

import frappe
from frappe import scrub

from support_ai.diagnostics.code_search import can_read_code, redact
from support_ai.diagnostics.indexer import get_doctype_controller_snippet, get_installed_custom_apps

DEFAULT_BUDGET = 6000
PER_FUNCTION_CHARS = 1500
PER_SCRIPT_CHARS = 1200
# Question words -> the doc_events they trigger.
EVENT_WORDS = {
	"submit": ("before_submit", "on_submit"),
	"cancel": ("before_cancel", "on_cancel"),
	"save": ("before_validate", "validate", "before_save", "on_update"),
	"update": (
		"before_validate",
		"validate",
		"before_save",
		"on_update",
		"before_update_after_submit",
		"on_update_after_submit",
	),
	"create": ("before_insert", "after_insert", "validate"),
	"delete": ("on_trash", "after_delete"),
	"amend": ("before_cancel", "on_cancel", "before_insert"),
}
TRIM_FIRST = {
	"custom_fields",
	"property_setters",
	"custom_reports",
	"notifications",
	"webhooks",
	"assignment_rules",
}
DOCTYPE_HOOKS = ("override_doctype_class", "doctype_js", "doctype_list_js")
PERMISSION_HOOKS = ("permission_query_conditions", "has_permission")


def build_customization_map(doctype, budget=None, focus=None):
	"""Customization map of a DocType, trimmed to a character budget.

	Parameters:
		doctype (str, required): DocType to describe.
		budget (int, optional): Max characters of code; defaults to Settings > Code Context Size.
		focus (str, optional): The user's question; hooks for the event it names ("submit",
			"cancel", "save") get their code first.

	Returns:
		dict: Section name -> content, plus "_notes" on what was left out.
	"""
	if not frappe.db.exists("DocType", doctype):
		return {"error": f"{doctype!r} is not a DocType."}
	budget = (
		budget or frappe.get_cached_doc("Support AI Settings").get("code_context_chars") or DEFAULT_BUDGET
	)
	with_code = can_read_code()
	builder = _MapBuilder(doctype, budget, with_code, _focus_events(focus))
	return builder.build()


class _MapBuilder:
	"""Fills map sections in priority order within a character budget."""

	def __init__(self, doctype, budget, with_code, focus_events=()):
		"""Starts an empty map for doctype."""
		self.doctype, self.budget, self.with_code = doctype, int(budget), with_code
		self.focus_events = set(focus_events)
		self.used = 0
		self.skipped = []
		self.custom_apps = get_installed_custom_apps()

	def build(self):
		"""Runs every section; code-heavy sections come first because they
		explain most "why does it do this" questions."""
		out = {"doctype": self.doctype, "custom_apps_considered": self.custom_apps}
		out["doc_event_hooks"] = self._with_sources(self._doc_event_handlers())
		out["server_scripts"] = self._scripts(
			"Server Script", {"reference_doctype": self.doctype, "disabled": 0}
		)
		out["overrides"] = self._with_sources(self._doctype_hooks())
		out["permission_hooks"] = self._with_sources(self._permission_hooks())
		out["whitelisted_method_overrides"] = self._with_sources(self._method_overrides())
		out["controller"] = self._controller()
		out["client_scripts"] = self._scripts("Client Script", {"dt": self.doctype, "enabled": 1})
		out.update(self._configuration())
		out = {k: v for k, v in out.items() if v not in (None, [], {})}
		self._fit(out)
		out["_notes"] = self._notes()
		return out

	# --- hooks.py ---------------------------------------------------------------

	def _doc_event_handlers(self):
		"""doc_events handlers of custom apps for this DocType and for "*"."""
		rows = []
		for app in self.custom_apps:
			doc_events = _app_hook(app, "doc_events") or {}
			for key in (self.doctype, "*"):
				rows += [
					{"app": app, "applies_to": key, "event": event, "handler": handler}
					for event, handlers in (doc_events.get(key) or {}).items()
					for handler in _as_list(handlers)
				]
		# Hooks of the event the user asked about get the code budget first.
		return sorted(rows, key=lambda row: row["event"] not in self.focus_events)

	def _doctype_hooks(self):
		"""Class overrides and form/list JS that custom apps attach to this DocType."""
		rows = []
		for app in self.custom_apps:
			for hook in DOCTYPE_HOOKS:
				target = (_app_hook(app, hook) or {}).get(self.doctype)
				rows += [{"app": app, "hook": hook, "handler": t} for t in _as_list(target)]
		return rows

	def _permission_hooks(self):
		"""Custom list/document permission rules for this DocType."""
		rows = []
		for app in self.custom_apps:
			for hook in PERMISSION_HOOKS:
				target = (_app_hook(app, hook) or {}).get(self.doctype)
				rows += [{"app": app, "hook": hook, "handler": t} for t in _as_list(target)]
		return rows

	def _method_overrides(self):
		"""override_whitelisted_methods whose original method belongs to this
		DocType's module path (e.g. erpnext...purchase_invoice.make_debit_note)."""
		marker = f".{scrub(self.doctype)}."
		rows = []
		for app in self.custom_apps:
			for original, target in (_app_hook(app, "override_whitelisted_methods") or {}).items():
				if marker in f"{original}." or marker in f"{target}.":
					rows += [
						{
							"app": app,
							"hook": "override_whitelisted_methods",
							"original": original,
							"handler": t,
						}
						for t in _as_list(target)
					]
		return rows

	def _with_sources(self, rows):
		"""Adds the handler's source code to each row while budget remains."""
		for row in rows:
			if row["handler"].endswith(".js") or not self.with_code:
				continue
			source = _function_source(row["handler"])
			if source and self._take(len(source)):
				row["source"] = source
			elif source:
				self.skipped.append(f"source of {row['handler']}")
		return rows

	# --- database code and configuration -----------------------------------------

	def _scripts(self, doctype, filters):
		"""Server/Client Scripts on this DocType, with code while budget remains."""
		fields = ["name", "script"] + (
			["script_type", "doctype_event"] if doctype == "Server Script" else ["view"]
		)
		rows = frappe.get_all(doctype, filters=filters, fields=fields)
		for row in rows:
			script = redact(row.pop("script") or "")[:PER_SCRIPT_CHARS]
			if self.with_code and self._take(len(script)):
				row["script"] = script
			elif self.with_code:
				self.skipped.append(f"{doctype} {row.name}")
		return rows

	def _controller(self):
		"""The DocType's own .py controller when it lives in a custom app."""
		if not self.with_code:
			return None
		snippet = get_doctype_controller_snippet(self.doctype, max_chars=PER_FUNCTION_CHARS * 2)
		if snippet and self._take(len(snippet["source"] or "")):
			snippet["source"] = redact(snippet["source"])
			return snippet
		return None

	def _configuration(self):
		"""No-code customizations: fields, workflow, notifications, ..."""
		dt = self.doctype
		return {
			"custom_fields": [_compact_field(f) for f in frappe.get_all(
				"Custom Field", {"dt": dt},
				["fieldname", "label", "fieldtype", "reqd", "depends_on", "mandatory_depends_on", "read_only_depends_on"],
			)],
			"property_setters": [
				f"{p.field_name or dt}.{p.property} = {str(p.value)[:80]}"
				for p in frappe.get_all("Property Setter", {"doc_type": dt}, ["field_name", "property", "value"])
			],
			"workflow": _workflow(dt),
			"notifications": frappe.get_all(
				"Notification", {"document_type": dt, "enabled": 1}, ["name", "event", "condition", "channel"]
			),
			"webhooks": frappe.get_all("Webhook", {"webhook_doctype": dt, "enabled": 1}, ["name", "webhook_docevent", "request_url"]),
			"assignment_rules": frappe.get_all("Assignment Rule", {"document_type": dt, "disabled": 0}, ["name", "rule", "priority"]),
			"user_permission_count": frappe.db.count("User Permission", {"allow": dt}),
			"custom_reports": frappe.get_all(
				"Report", {"ref_doctype": dt, "is_standard": "No", "disabled": 0}, pluck="name", limit_page_length=20
			),
		}  # fmt: skip

	def _fit(self, out):
		"""Halves list sections until the whole map fits about twice the code
		budget. Configuration lists (a DocType can have hundreds of custom
		fields) are cut before code sections, whose size the budget already
		limits."""
		limit = self.budget * 2
		while len(json.dumps(out, default=str)) > limit:
			key = self._largest_trimmable(out)
			if not key:
				break
			keep = len(out[key]) // 2
			self.skipped.append(f"{len(out[key]) - keep} of {len(out[key])} {key}")
			out[key] = out[key][:keep]

	def _largest_trimmable(self, out):
		"""Largest list section that can still be halved, configuration first."""
		for group in (TRIM_FIRST, None):
			lists = [
				(len(json.dumps(v, default=str)), k)
				for k, v in out.items()
				if isinstance(v, list) and len(v) > 1 and (group is None or k in group)
			]
			if lists:
				return max(lists)[1]
		return None

	def _take(self, size):
		"""Reserves size characters of the budget; False when it is spent."""
		if self.used + size > self.budget:
			return False
		self.used += size
		return True

	def _notes(self):
		"""What a reader of the map should know about omissions."""
		notes = []
		if not self.with_code:
			notes.append("Source code omitted: the user lacks the Support AI code access role.")
		if self.skipped:
			notes.append(
				f"Left out to fit the size limit ({self.budget} chars): " + ", ".join(self.skipped[:10])
			)
		return notes


def _focus_events(question):
	"""doc_events named by the question's words ("submitting" -> on_submit)."""
	words = (question or "").lower()
	return {event for word, events in EVENT_WORDS.items() if word in words for event in events}


def _compact_field(field):
	"""'fieldname (Type)' for plain custom fields; a dict only when the field
	has rules (mandatory / depends_on) that can explain behaviour."""
	rules = {
		k: v
		for k, v in field.items()
		if k in ("reqd", "depends_on", "mandatory_depends_on", "read_only_depends_on") and v
	}
	if not rules:
		return f"{field.fieldname} ({field.fieldtype})"
	return {"field": f"{field.fieldname} ({field.fieldtype})", **rules}


def _workflow(doctype):
	"""Active workflow: states and transitions with the role allowed for each."""
	name = frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1}, "name")
	if not name:
		return None
	workflow = frappe.get_cached_doc("Workflow", name)
	return {
		"name": name,
		"state_field": workflow.workflow_state_field,
		"transitions": [
			{
				"from": t.state,
				"action": t.action,
				"to": t.next_state,
				"role": t.allowed,
				"condition": t.condition,
			}
			for t in workflow.transitions
		],
	}


def _app_hook(app, hook):
	"""One hook from one app's hooks.py (dicts are merged by Frappe)."""
	try:
		return frappe.get_hooks(hook, app_name=app)
	except Exception:
		return None


def _as_list(value):
	"""Hooks may hold a string or a list of strings."""
	if not value:
		return []
	return value if isinstance(value, list) else [value]


def _function_source(dotted_path):
	"""Source of a hook target (function or class), redacted and trimmed."""
	try:
		target = frappe.get_attr(dotted_path)
		source = inspect.getsource(target)
		where = (
			f"# {os.path.relpath(inspect.getsourcefile(target), frappe.get_app_path('frappe', '..', '..'))}\n"
		)
	except Exception:
		return None
	text = redact(where + source)
	if len(text) > PER_FUNCTION_CHARS:
		text = text[:PER_FUNCTION_CHARS] + f"\n# ... ({len(text) - PER_FUNCTION_CHARS} more chars)"
	return text
