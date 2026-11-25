# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Logic behind the chat's one-click actions: feedback on an answer, CSV
export of a list answer, role-based suggested questions, and confirming or
cancelling a write action the model proposed. Whitelisted wrappers live in
support_ai/support_ai/api/v1/assistant.py."""

import frappe
from frappe import _

from support_ai.diagnostics.masking import mask_record

MAX_EXPORT_ROWS = 5000

# Built-in starter questions per role; Settings > Suggested Questions adds more.
ROLE_SUGGESTIONS = {
	"Accounts User": ["Overdue purchase invoices this month", "Total outstanding of suppliers"],
	"Accounts Manager": [
		"Accounts receivable outstanding of customers",
		"Count of journal entries last month",
	],
	"Purchase User": ["Latest 5 purchase orders", "Purchase receipts this week"],
	"Sales User": ["Latest 5 sales orders", "Sales invoices this month above 1 lakh"],
	"Stock User": ["Latest 5 stock entries", "Material requests this week"],
	"HR User": ["Employees who joined this month"],
}
EVERYONE_SUGGESTIONS = ["What needs my attention?"]


def set_feedback(conversation, message_index, feedback, comment=None):
	"""Stores 👍 / 👎 (and an optional comment) on one assistant message.

	Parameters:
		conversation (str, required): Support AI Conversation name.
		message_index (int, required): 0-based index of the message.
		feedback (str, required): "Up", "Down" or "" to clear.
		comment (str, optional): Why the answer was wrong.

	Returns:
		dict: {"feedback": str}
	"""
	if feedback not in ("Up", "Down", ""):
		frappe.throw(_("Feedback must be Up or Down."))
	convo = frappe.get_doc("Support AI Conversation", conversation)
	convo.check_permission("write")
	index = frappe.utils.cint(message_index)
	if index < 0 or index >= len(convo.messages) or convo.messages[index].role != "Assistant":
		frappe.throw(_("That message cannot be rated."))
	row = convo.messages[index]
	row.feedback = feedback or None
	row.feedback_comment = (comment or "").strip()[:1000] or None
	convo.save(ignore_permissions=False)
	return {"feedback": row.feedback}


def export_records(doctype, filters=None, fields=None):
	"""Rows for a CSV download of a list answer, via get_list (so the user's
	permissions apply), with sensitive fields masked.

	Parameters:
		doctype (str, required): DocType to export.
		filters (dict | list, optional): Same filters the answer used.
		fields (list[str], optional): Columns; invalid names are dropped.

	Returns:
		dict: {"columns": [labels], "rows": [[values]]}
	"""
	if not frappe.has_permission(doctype, "export") and not frappe.has_permission(doctype, "read"):
		frappe.throw(_("You do not have permission to export {0}.").format(doctype), frappe.PermissionError)
	meta = frappe.get_meta(doctype)
	valid = {df.fieldname for df in meta.fields if df.fieldtype not in ("Table", "Table MultiSelect")}
	valid |= {"name", "owner", "creation", "modified", "docstatus"}
	fields = [f for f in (fields or ["name"]) if f in valid] or ["name"]

	rows = frappe.get_list(
		doctype,
		filters=filters or {},
		fields=fields,
		order_by="creation desc",
		limit_page_length=MAX_EXPORT_ROWS,
	)
	rows = mask_record(rows)
	columns = [
		"ID" if f == "name" else meta.get_label(f) if meta.get_field(f) else frappe.unscrub(f) for f in fields
	]
	return {"columns": columns, "rows": [[r.get(f) for f in fields] for r in rows]}


def get_suggestions():
	"""Starter questions for the current user's roles: Settings rows first,
	then built-in ones, at most 6.

	Returns:
		list[str]
	"""
	roles = set(frappe.get_roles())
	settings = frappe.get_cached_doc("Support AI Settings")
	configured = [r.prompt for r in settings.get("suggested_prompts") or [] if not r.role or r.role in roles]
	built_in = [p for role, prompts in ROLE_SUGGESTIONS.items() if role in roles for p in prompts]
	return list(dict.fromkeys(configured + EVERYONE_SUGGESTIONS + built_in))[:6]


def resolve_write_action(action_id, confirm):
	"""Runs (on Confirm) or drops (on Cancel) a write action the model
	proposed, and records the outcome as a message in the conversation.

	Parameters:
		action_id (str, required): Id from the confirm card.
		confirm (bool, required): True to run it, False to cancel.

	Returns:
		dict: {"reply": str, "message_index": int}
	"""
	key = f"support_ai_pending_action:{action_id}"
	pending = frappe.cache.get_value(key)
	if not pending or pending.get("user") != frappe.session.user:
		frappe.throw(_("This action has expired or was already handled. Ask again to get a new one."))
	frappe.cache.delete_value(key)

	convo = frappe.get_doc("Support AI Conversation", pending["conversation"])
	convo.check_permission("write")
	reply = _run_pending(convo, pending) if confirm else _("Cancelled - nothing was changed.")
	convo.append_message("Assistant", reply, answered_by="LLM")
	convo.save(ignore_permissions=False)
	return {"reply": reply, "message_index": len(convo.messages) - 1}


def _run_pending(convo, pending):
	"""Runs a confirmed write tool and returns a one-line outcome."""
	from support_ai.ai.llm import get_llm_config
	from support_ai.ai.tools import ToolContext, get_tools, run_tool
	from support_ai.api import get_bot

	bot = get_bot(convo.bot) if convo.bot else None
	tools = {t.name: t for t in get_tools(bot, bool(convo.reference_name), get_llm_config(bot))}
	spec = tools.get(pending["tool"])
	if not spec:
		return _("This action is no longer available to you.")
	ctx = ToolContext(bot=bot, conversation=convo.name, confirmed=True)
	result = run_tool(spec, pending["args"], ctx)
	if isinstance(result, dict) and result.get("error"):
		return _("❌ Failed: {0}").format(result["error"])
	message = result.get("message") if isinstance(result, dict) else None
	return _("✅ Done: {0}").format(message or spec.name)
