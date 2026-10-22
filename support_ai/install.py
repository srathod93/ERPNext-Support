# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

import frappe

from support_ai.ai.functions import DEFAULT_WRITE_ROLE


def after_install():
	ensure_write_role()


def after_migrate():
	ensure_write_role()


def ensure_write_role():
	"""The role that lets a user have Support AI change data on their behalf.
	Assign it to users from User > Roles."""
	if not frappe.db.exists("Role", DEFAULT_WRITE_ROLE):
		frappe.get_doc(
			{"doctype": "Role", "role_name": DEFAULT_WRITE_ROLE, "desk_access": 1, "is_custom": 1}
		).insert(ignore_permissions=True)

	if not frappe.db.get_single_value("Support AI Settings", "write_action_role"):
		frappe.db.set_single_value("Support AI Settings", "write_action_role", DEFAULT_WRITE_ROLE)
