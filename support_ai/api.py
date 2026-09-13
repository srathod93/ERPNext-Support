# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _

from support_ai.ai.llm import LLMError, chat
from support_ai.diagnostics.context import build_document_context
from support_ai.diagnostics.indexer import get_installed_custom_apps_overview


@frappe.whitelist()
def new_conversation(reference_doctype=None, reference_name=None, title=None):
	if reference_doctype and reference_name:
		if not frappe.has_permission(reference_doctype, "read", doc=reference_name):
			frappe.throw(_("You do not have permission to read {0} {1}").format(reference_doctype, reference_name))
		title = title or f"{reference_doctype} {reference_name}"

	conversation = frappe.get_doc(
		{
			"doctype": "Support AI Conversation",
			"title": title or "New Conversation",
			"user": frappe.session.user,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		}
	).insert(ignore_permissions=False)

	return conversation.name


@frappe.whitelist()
def ask(conversation, message, reference_doctype=None, reference_name=None):
	if not message or not message.strip():
		frappe.throw(_("Message cannot be empty."))

	convo = frappe.get_doc("Support AI Conversation", conversation)
	convo.check_permission("write")

	reference_doctype = reference_doctype or convo.reference_doctype
	reference_name = reference_name or convo.reference_name

	convo.append_message("User", message)

	diagnostic_context = build_context_payload(reference_doctype, reference_name)

	settings = frappe.get_cached_doc("Support AI Settings")
	llm_messages = [{"role": "system", "content": settings.system_prompt}]
	if diagnostic_context is not None:
		llm_messages.append(
			{
				"role": "system",
				"content": "Diagnostic context (read-only, JSON):\n" + json.dumps(diagnostic_context, default=str, indent=2),
			}
		)
	llm_messages.extend(convo.as_chat_history())

	try:
		reply = chat(llm_messages)
	except LLMError as e:
		reply = f"⚠️ Support AI could not get a response: {e}"

	convo.append_message("Assistant", reply)
	convo.save(ignore_permissions=False)

	return {"conversation": convo.name, "reply": reply}


def build_context_payload(reference_doctype, reference_name):
	if reference_doctype and reference_name:
		return build_document_context(reference_doctype, reference_name)
	return {"note": "No specific document referenced.", "installed_custom_apps": get_installed_custom_apps_overview()}


@frappe.whitelist()
def list_conversations():
	return frappe.get_list(
		"Support AI Conversation",
		filters={"user": frappe.session.user},
		fields=["name", "title", "status", "reference_doctype", "reference_name", "modified"],
		order_by="modified desc",
		limit_page_length=50,
	)


@frappe.whitelist()
def get_conversation(name):
	convo = frappe.get_doc("Support AI Conversation", name)
	convo.check_permission("read")
	return convo
