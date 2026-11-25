# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Helpers that tell core apps from this site's custom apps, and read a
DocType's controller source when it lives in a custom app. The full
per-DocType picture is built by customization_map.py; text search over
custom code is code_search.py."""

import os

import frappe
from frappe import scrub
from frappe.modules.utils import get_module_app

DEFAULT_CORE_APPS = {"frappe", "erpnext", "hrms", "payments", "india_compliance", "support_ai"}


def get_core_apps():
	"""Apps treated as core (not scanned): Settings > Core Apps, else the default set."""
	settings = frappe.get_cached_doc("Support AI Settings")
	custom = (settings.core_apps or "").strip()
	if custom:
		return {a.strip() for a in custom.split(",") if a.strip()}
	return DEFAULT_CORE_APPS


def get_installed_custom_apps():
	"""Installed apps that are not core - the ones whose code Support AI reads."""
	core = get_core_apps()
	return [app for app in frappe.get_installed_apps() if app not in core]


def truncate(text, max_chars=1500):
	"""Cuts text to max_chars and says how much was left out."""
	if not text:
		return text
	if len(text) <= max_chars:
		return text
	return text[:max_chars] + f"\n... (truncated, {len(text) - max_chars} more chars)"


def get_doctype_controller_snippet(doctype, max_chars=3000):
	"""{"app", "path", "source"} of the DocType's .py controller if it lives in a custom app, else None."""
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
