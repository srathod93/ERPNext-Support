# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Masks sensitive field values (PAN, bank account, salary, ...) in anything
Support AI shows or sends to a model. Read permission on a document is not
enough to see these - the user also needs the unmask role from Support AI
Settings. Fieldnames are matched by substring, so "pan" also covers
"pan_no" and "supplier_pan"."""

import frappe

MASK = "••••••"


def _settings():
	"""Returns (patterns, unmask_role) from Support AI Settings."""
	settings = frappe.get_cached_doc("Support AI Settings")
	raw = settings.get("sensitive_fieldnames") or ""
	patterns = [p.strip().lower() for p in raw.splitlines() if p.strip()]
	return patterns, settings.get("sensitive_unmask_role")


def can_see_sensitive():
	"""True when the current user may see sensitive values unmasked."""
	patterns, role = _settings()
	if not patterns:
		return True
	return bool(role and role in frappe.get_roles())


def is_sensitive(fieldname, patterns=None):
	"""True when fieldname matches one of the configured sensitive patterns.

	Parameters:
		fieldname (str): The field to check.
		patterns (list[str], optional): Pre-loaded patterns, to avoid re-reading settings.
	"""
	if not fieldname:
		return False
	if patterns is None:
		patterns, _role = _settings()
	name = fieldname.lower()
	# "pan" must not match "company" - require a word boundary in the fieldname.
	parts = name.split("_")
	return any(
		p == name or p in parts or name.startswith(p + "_") or name.endswith("_" + p) for p in patterns
	)


def mask_value(value):
	"""Keeps the last 2 characters so users can still tell values apart."""
	if value in (None, ""):
		return value
	text = str(value)
	return MASK + text[-2:] if len(text) > 4 else MASK


def mask_record(record):
	"""Returns a copy of a dict (or list of dicts, recursively) with sensitive
	values masked, unless the current user has the unmask role."""
	if can_see_sensitive():
		return record
	patterns, _role = _settings()
	return _mask(record, patterns)


def _mask(value, patterns):
	"""Recursive worker for mask_record."""
	if isinstance(value, list):
		return [_mask(v, patterns) for v in value]
	if isinstance(value, dict):
		out = type(value)() if isinstance(value, frappe._dict) else {}
		for k, v in value.items():
			out[k] = (
				mask_value(v)
				if is_sensitive(k, patterns) and not isinstance(v, (dict, list))
				else _mask(v, patterns)
			)
		return out
	return value
