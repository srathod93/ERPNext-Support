# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Shared test data builders for the Support AI tests. Everything is created
inside the test transaction, which IntegrationTestCase rolls back."""

import json

import frappe

TEST_EMAIL = "support-ai-router-test@example.com"
HAS_ERPNEXT = "erpnext" in frappe.get_installed_apps()


def make_test_user(email=TEST_EMAIL, roles=None):
	"""Creates (or reuses) a System User, optionally with extra roles.

	Parameters:
		email (str, optional): User ID.
		roles (list[str], optional): Roles to add.

	Returns:
		str: The user name.
	"""
	if not frappe.db.exists("User", email):
		# Some sites have a server script that requires a last name.
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Router",
				"last_name": "Test",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
	if roles:
		user = frappe.get_doc("User", email)
		user.add_roles(*roles)
	return email


def make_role(role_name):
	"""Creates (or reuses) a desk Role.

	Parameters:
		role_name (str, required): Role name, e.g. "SAI-TEST-01".

	Returns:
		str: The role name.
	"""
	if not frappe.db.exists("Role", role_name):
		frappe.get_doc({"doctype": "Role", "role_name": role_name, "desk_access": 1}).insert(
			ignore_permissions=True
		)
	return role_name


def make_todo(description, owner=None, allocated_to=None):
	"""Creates an open ToDo, optionally owned by / allocated to a user.

	Parameters:
		description (str, required): ToDo text.
		owner (str, optional): Creator to set (insert always uses the session user).
		allocated_to (str, optional): Assignee.

	Returns:
		Document: The ToDo.
	"""
	todo = frappe.get_doc(
		{"doctype": "ToDo", "description": description, "allocated_to": allocated_to}
	).insert(ignore_permissions=True)
	if owner:
		frappe.db.set_value("ToDo", todo.name, "owner", owner, update_modified=False)
		todo.owner = owner
	return todo


def make_conversation(messages=None, title="Support AI test"):
	"""Creates a conversation for the session user with optional messages.

	Parameters:
		messages (list[dict], optional): Rows like {"role": "User", "content": "hi"}.
		title (str, optional): Conversation title.

	Returns:
		Document: The Support AI Conversation.
	"""
	convo = frappe.get_doc(
		{"doctype": "Support AI Conversation", "title": title, "user": frappe.session.user, "messages": []}
	)
	for message in messages or []:
		convo.append_message(message.pop("role"), message.pop("content"), **message)
	convo.insert(ignore_permissions=True)
	return convo


def actions_of(message_row):
	"""Actions stored on an assistant message's response_data."""
	return json.loads(message_row.response_data or "{}").get("actions") or []


def clear_settings_cache():
	"""Drops the cached Support AI Settings after a test changed them."""
	frappe.clear_document_cache("Support AI Settings", "Support AI Settings")
