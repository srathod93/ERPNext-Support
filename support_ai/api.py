# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _

from support_ai.ai.llm import LLMError, chat_message
from support_ai.diagnostics.context import build_document_context
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
		reply = run_chat(llm_messages, tools=tools)
	except LLMError as e:
		reply = f"⚠️ Support AI could not get a response: {e}"

	convo.append_message("Assistant", reply)
	convo.save(ignore_permissions=False)

	return {"conversation": convo.name, "reply": reply}


MAX_TOOL_ROUNDS = 2
MAX_TOOL_RESULT_CHARS = 3000


def run_chat(llm_messages, tools=None):
	"""Runs a bounded tool-call loop: the model can call lookup_records more
	than once (e.g. once for GL Entry, once for Journal Entry) before giving
	its final answer. Tool results are fed back as plain messages rather than
	provider-specific tool_call/tool-role replay, which differs enough
	between Ollama and OpenAI-compatible APIs to be a fragile way to get
	what is, in the end, just extra context for the next turn.

	Critically, the message list sent each round is rebuilt from scratch as
	base_messages + a capped tool-context block, rather than appended to -
	on a CPU-only local model, letting tool results accumulate round over
	round can push the prompt past the model's context window, which was
	observed to blow up both memory (~5GB RSS for a 3B model) and latency
	(a single round exceeding 3 minutes) as the runtime pages old context out
	and back in. Capping and rebuilding keeps every round's prompt bounded."""
	base_messages = list(llm_messages)
	original_question = next(
		(m.get("content") for m in reversed(llm_messages) if m.get("role") == "user"), None
	)
	seen_calls = set()
	all_results = []

	for _round in range(MAX_TOOL_ROUNDS):
		messages = base_messages + _tool_context_messages(all_results, original_question)
		assistant_message = chat_message(messages, tools=tools)
		tool_calls = assistant_message.get("tool_calls") or []

		if not tool_calls:
			return assistant_message.get("content") or "(no response)"

		made_new_call = False
		for call in tool_calls:
			if call.get("name") != "lookup_records":
				continue

			args = call.get("arguments") or {}
			key = json.dumps(args, sort_keys=True, default=str)
			if key in seen_calls:
				continue
			seen_calls.add(key)
			made_new_call = True

			try:
				result = lookup_records(**args)
			except Exception as e:
				result = {"error": str(e)}

			all_results.append({"called_with": args, "result": result})

		if not made_new_call:
			# Model tried to repeat a call (or something unsupported) instead of
			# answering. If we already have real results, force a synthesis of
			# those below rather than returning a content-free filler line.
			break

	if not all_results:
		return "I wasn't able to look that up. Could you rephrase, naming the exact DocType and field you need?"

	# Force a final answer (tools off) from whatever's been gathered so far,
	# whether we hit the round cap or the model stalled on a repeat call.
	final_messages = base_messages + _tool_context_messages(all_results, original_question)
	final = chat_message(final_messages)
	return final.get("content") or "(no response)"


def _tool_context_messages(all_results, original_question):
	if not all_results:
		return []

	content = json.dumps(all_results, default=str, indent=2)
	if len(content) > MAX_TOOL_RESULT_CHARS:
		content = (
			content[:MAX_TOOL_RESULT_CHARS]
			+ f"\n... (truncated, {len(content) - MAX_TOOL_RESULT_CHARS} more chars - ask a narrower "
			"question, e.g. a smaller limit or an extra filter, for more detail)"
		)

	messages = [{"role": "system", "content": "Tool results so far:\n" + content}]
	if original_question:
		messages.append(
			{"role": "user", "content": f"Using the tool results above, answer my original question: {original_question}"}
		)
	return messages


def build_context_payload(reference_doctype, reference_name):
	if reference_doctype and reference_name:
		return build_document_context(reference_doctype, reference_name)
	# No document referenced: don't preload the full custom-apps/doctypes
	# overview here - on a small-context local model that alone can be a
	# large fraction of the available prompt budget, and the lookup_records
	# tool already lets the model query real doctypes/data on demand instead.
	return {"note": "No specific document referenced. Use the lookup_records tool for any data question."}


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
