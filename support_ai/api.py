# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _

from support_ai.ai.llm import LLMError, chat_message
from support_ai.diagnostics.context import build_document_context
from support_ai.diagnostics.indexer import get_installed_custom_apps_overview
from support_ai.diagnostics.lookup import LOOKUP_TOOL_SCHEMA, lookup_records


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

	has_reference = bool(reference_doctype and reference_name)
	diagnostic_context = build_context_payload(reference_doctype, reference_name)

	from support_ai.support_ai.doctype.support_ai_settings.support_ai_settings import get_system_prompt

	llm_messages = [{"role": "system", "content": get_system_prompt()}]
	if diagnostic_context is not None:
		llm_messages.append(
			{
				"role": "system",
				"content": "Diagnostic context (read-only, JSON):\n" + json.dumps(diagnostic_context, default=str, indent=2),
			}
		)
	llm_messages.extend(convo.as_chat_history())

	# The lookup tool only makes sense for general questions - when a specific
	# document is already referenced we already have its real data in context,
	# and offering the tool there just invites the model to go looking elsewhere.
	tools = None if has_reference else [LOOKUP_TOOL_SCHEMA]

	try:
		reply = run_chat(llm_messages, message, tools=tools)
	except LLMError as e:
		reply = f"⚠️ Support AI could not get a response: {e}"

	convo.append_message("Assistant", reply)
	convo.save(ignore_permissions=False)

	return {"conversation": convo.name, "reply": reply}


def run_chat(llm_messages, user_message, tools=None):
	assistant_message = chat_message(llm_messages, tools=tools)
	tool_calls = assistant_message.get("tool_calls") or []

	if not tool_calls:
		return assistant_message.get("content") or "(no response)"

	call = tool_calls[0]
	if call.get("name") != "lookup_records":
		return assistant_message.get("content") or "(no response)"

	try:
		result = lookup_records(**(call.get("arguments") or {}))
	except Exception as e:
		result = {"error": str(e)}

	# A fresh, isolated exchange to summarize the tool result in natural
	# language - avoids replaying provider-specific tool_call/tool-role
	# message formats, which differ enough between Ollama and OpenAI-
	# compatible APIs to be a source of fragile bugs for little benefit here.
	summarize_messages = [
		{
			"role": "system",
			"content": (
				"You just looked up data for the user via a read-only tool. Answer their question "
				"directly using ONLY the JSON data given below - never add anything not present in it. "
				"If the data has an 'error' key, explain that plainly instead."
			),
		},
		{
			"role": "user",
			"content": f"Original question: {user_message}\n\nLookup result (JSON):\n{json.dumps(result, default=str, indent=2)}",
		},
	]
	final = chat_message(summarize_messages)
	return final.get("content") or json.dumps(result, default=str)


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
