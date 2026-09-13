# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Lightweight, on-demand indexer over installed custom apps. No embeddings or
vector DB required: for a given DocType we scope the search down to exactly
what could be responsible for its behaviour - hooks.py doc_events, Server
Scripts, Client Scripts, Custom Fields, Property Setters, and the DocType's
own controller file if it lives in a custom app."""

import os

import frappe
from frappe import scrub
from frappe.modules.utils import get_module_app

DEFAULT_CORE_APPS = {"frappe", "erpnext", "hrms", "payments", "india_compliance", "support_ai"}


def get_core_apps():
	settings = frappe.get_cached_doc("Support AI Settings")
	custom = (settings.core_apps or "").strip()
	if custom:
		return {a.strip() for a in custom.split(",") if a.strip()}
	return DEFAULT_CORE_APPS


def get_installed_custom_apps():
	core = get_core_apps()
	return [app for app in frappe.get_installed_apps() if app not in core]


def truncate(text, max_chars=1500):
	if not text:
		return text
	if len(text) <= max_chars:
		return text
	return text[:max_chars] + f"\n... (truncated, {len(text) - max_chars} more chars)"


def get_doctype_custom_code_context(doctype):
	return {
		"custom_apps_considered": get_installed_custom_apps(),
		"controller_source": get_doctype_controller_snippet(doctype),
		"hook_handlers": get_hook_handlers(doctype),
		"server_scripts": get_server_scripts(doctype),
		"client_scripts": get_client_scripts(doctype),
		"custom_fields": frappe.get_all(
			"Custom Field",
			filters={"dt": doctype},
			fields=["fieldname", "label", "fieldtype", "mandatory_depends_on", "depends_on", "read_only_depends_on"],
		),
		"property_setters": frappe.get_all(
			"Property Setter",
			filters={"doc_type": doctype},
			fields=["property", "value", "field_name"],
		),
	}


def get_doctype_controller_snippet(doctype, max_chars=3000):
	try:
		meta = frappe.get_meta(doctype)
		module = meta.module
	except Exception:
		return None

	if not module:
		return None

	try:
		app = get_module_app(module)
	except Exception:
		return None

	if app in get_core_apps():
		return None

	try:
		module_path = frappe.get_module_path(module)
		controller_path = os.path.join(module_path, "doctype", scrub(doctype), scrub(doctype) + ".py")
		if not os.path.isfile(controller_path):
			return None
		with open(controller_path) as f:
			source = f.read()
	except Exception:
		return None

	return {"app": app, "path": controller_path, "source": truncate(source, max_chars)}


def get_hook_handlers(doctype):
	handlers = []
	core = get_core_apps()
	for app in frappe.get_installed_apps():
		if app in core:
			continue
		try:
			doc_events = frappe.get_hooks("doc_events", app_name=app) or {}
		except Exception:
			continue
		for key in (doctype, "*"):
			events = doc_events.get(key)
			if not events:
				continue
			for event, handler in events.items():
				handler_list = handler if isinstance(handler, list) else [handler]
				for h in handler_list:
					handlers.append({"app": app, "event": event, "handler": h})
	return handlers


def get_server_scripts(doctype, limit=5, max_chars=1500):
	rows = frappe.get_all(
		"Server Script",
		filters={"reference_doctype": doctype, "disabled": 0},
		fields=["name", "script_type", "doctype_event", "script"],
		limit_page_length=limit,
	)
	for r in rows:
		r["script"] = truncate(r.get("script"), max_chars)
	return rows


def get_client_scripts(doctype, limit=5, max_chars=1500):
	rows = frappe.get_all(
		"Client Script",
		filters={"dt": doctype, "enabled": 1},
		fields=["name", "view", "script"],
		limit_page_length=limit,
	)
	for r in rows:
		r["script"] = truncate(r.get("script"), max_chars)
	return rows


def get_installed_custom_apps_overview():
	overview = []
	for app in get_installed_custom_apps():
		modules = frappe.get_all("Module Def", filters={"app_name": app}, pluck="name")
		doctypes = []
		if modules:
			doctypes = frappe.get_all(
				"DocType", filters={"module": ["in", modules], "custom": 0}, pluck="name", limit_page_length=50
			)
		overview.append({"app": app, "modules": modules, "doctypes": doctypes})
	return overview
