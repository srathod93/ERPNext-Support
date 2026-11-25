# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

import frappe

from support_ai.ai.functions import DEFAULT_WRITE_ROLE


def after_install():
	"""Runs once when the app is installed on a site."""
	ensure_write_role()
	ensure_settings_defaults()


def after_migrate():
	"""Runs after every migrate, so settings added in an update get defaults."""
	ensure_write_role()
	ensure_settings_defaults()


def ensure_settings_defaults():
	"""Gives Support AI Settings fields added in an update their default value
	once. A Single's new field reads back empty until saved, which would
	silently turn e.g. streaming off - a value the admin set is never touched."""
	meta = frappe.get_meta("Support AI Settings")
	for fieldname in ("enable_streaming", "rate_limit_per_minute", "sensitive_fieldnames"):
		df = meta.get_field(fieldname)
		singles = frappe.qb.DocType("Singles")
		stored = (
			frappe.qb.from_(singles)
			.select(singles.value)
			.where((singles.doctype == "Support AI Settings") & (singles.field == fieldname))
			.run()
		)
		if df and df.default is not None and not stored:
			frappe.db.set_single_value("Support AI Settings", fieldname, df.default)


def ensure_write_role():
	"""The role that lets a user have Support AI change data on their behalf.
	Assign it to users from User > Roles."""
	if not frappe.db.exists("Role", DEFAULT_WRITE_ROLE):
		frappe.get_doc(
			{"doctype": "Role", "role_name": DEFAULT_WRITE_ROLE, "desk_access": 1, "is_custom": 1}
		).insert(ignore_permissions=True)

	if not frappe.db.get_single_value("Support AI Settings", "write_action_role"):
		frappe.db.set_single_value("Support AI Settings", "write_action_role", DEFAULT_WRITE_ROLE)
