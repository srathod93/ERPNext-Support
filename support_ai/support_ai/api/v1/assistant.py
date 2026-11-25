# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Whitelisted endpoints for the chat's one-click actions. Thin wrappers -
the logic lives in support_ai.ai.chat_actions."""

import frappe

from support_ai.ai import chat_actions


@frappe.whitelist(methods=["POST"])
def set_feedback(conversation: str, message_index: int, feedback: str, comment: str | None = None):
	"""
		Store 👍 / 👎 feedback (and an optional comment) on one assistant answer.
		Used by the usage report to find answers that need fixing.

		**Endpoint:** `/api/method/support_ai.support_ai.api.v1.assistant.set_feedback`
		**HTTP Method:** POST
		**Parameters:**
			- conversation (str, required): Support AI Conversation name
			- message_index (int, required): 0-based index of the answer in the conversation
			- feedback (str, required): "Up", "Down", or "" to clear
			- comment (str, optional): What was wrong with the answer
		**Response:**
	```json
			{"message": {"feedback": "Down"}}
	```
	"""
	return chat_actions.set_feedback(conversation, message_index, feedback, comment)


@frappe.whitelist(methods=["POST"])
def export_records(doctype: str, filters: str | None = None, fields: str | None = None):
	"""
		Return the rows of a list answer for a CSV download (up to 5000 rows).
		Runs through get_list, so the user's permissions apply; sensitive
		fields are masked.

		**Endpoint:** `/api/method/support_ai.support_ai.api.v1.assistant.export_records`
		**HTTP Method:** POST
		**Parameters:**
			- doctype (str, required): DocType to export
			- filters (str, optional): JSON filters, as used by the answer
			- fields (str, optional): JSON list of fieldnames
		**Response:**
	```json
			{"message": {"columns": ["ID", "Supplier"], "rows": [["ACC-PINV-2026-17381", "810000011"]]}}
	```
	"""
	return chat_actions.export_records(
		doctype, frappe.parse_json(filters or "{}"), frappe.parse_json(fields or "[]")
	)


@frappe.whitelist(methods=["GET", "POST"])
def get_suggestions():
	"""
		Return starter questions for the current user's roles, shown as
		one-click chips on a new chat.

		**Endpoint:** `/api/method/support_ai.support_ai.api.v1.assistant.get_suggestions`
		**HTTP Method:** GET
		**Parameters:** none
		**Response:**
	```json
			{"message": ["What needs my attention?", "Latest 5 purchase orders"]}
	```
	"""
	return chat_actions.get_suggestions()


@frappe.whitelist(methods=["POST"])
def resolve_write_action(action_id: str, confirm: int = 0):
	"""
		Run (confirm=1) or cancel (confirm=0) a data change the AI proposed.
		Nothing is written until the user confirms here.

		**Endpoint:** `/api/method/support_ai.support_ai.api.v1.assistant.resolve_write_action`
		**HTTP Method:** POST
		**Parameters:**
			- action_id (str, required): Id from the confirm card
			- confirm (int, optional): 1 to run, 0 to cancel (default)
		**Response:**
	```json
			{"message": {"reply": "✅ Done: Created Payment Entry PE-0001", "message_index": 5}}
	```
	"""
	return chat_actions.resolve_write_action(action_id, bool(frappe.utils.cint(confirm)))
