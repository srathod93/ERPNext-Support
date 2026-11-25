# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import strip_html_tags

from support_ai.ai.llm import get_settings
from support_ai.diagnostics.context import build_document_context

THREAD_SOURCE_DOCTYPES = ("Comment", "Communication")


# --- Bots -----------------------------------------------------------------------


def get_bot(bot_name, throw=True):
	"""Loads a bot the current user may use: enabled, and either open to
	everyone or shared with one of the user's roles."""
	if not bot_name:
		return None
	if not frappe.db.exists("Support AI Bot", bot_name):
		if throw:
			frappe.throw(_("Support AI Bot {0} does not exist.").format(bot_name))
		return None

	bot = frappe.get_cached_doc("Support AI Bot", bot_name)
	if not bot.enabled:
		if throw:
			frappe.throw(_("Support AI Bot {0} is disabled.").format(bot_name))
		return None
	if bot.allowed_roles and not ({r.role for r in bot.allowed_roles} & set(frappe.get_roles())):
		if throw:
			frappe.throw(_("You are not allowed to use Support AI Bot {0}.").format(bot_name), frappe.PermissionError)
		return None
	return bot


def get_default_bot_name():
	default = get_settings().get("default_bot")
	return default if default and get_bot(default, throw=False) else None


@frappe.whitelist()
def get_available_bots():
	bots = []
	if not frappe.db.table_exists("Support AI Bot"):
		# Site not migrated yet - behave like the plain diagnostic assistant.
		return {"bots": bots, "default": None}
	for name in frappe.get_all("Support AI Bot", filters={"enabled": 1}, pluck="name", order_by="bot_name asc"):
		bot = get_bot(name, throw=False)
		if bot:
			bots.append({"name": bot.name, "description": bot.description})
	return {"bots": bots, "default": get_default_bot_name()}


# --- Conversations --------------------------------------------------------------


@frappe.whitelist()
def new_conversation(
	reference_doctype=None, reference_name=None, title=None, bot=None, source_doctype=None, source_name=None
):
	source_note = None
	if source_doctype and source_name:
		source_note, reference_doctype, reference_name, source_title = _thread_source(source_doctype, source_name)
		title = title or source_title

	if reference_doctype and reference_name:
		if not frappe.has_permission(reference_doctype, "read", doc=reference_name):
			frappe.throw(_("You do not have permission to read {0} {1}").format(reference_doctype, reference_name))
		title = title or f"{reference_doctype} {reference_name}"

	bot = bot or get_default_bot_name()
	if bot:
		get_bot(bot)

	conversation = frappe.get_doc(
		{
			"doctype": "Support AI Conversation",
			"title": title or "New Conversation",
			"user": frappe.session.user,
			"bot": bot,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"source_doctype": source_doctype if source_note else None,
			"source_name": source_name if source_note else None,
		}
	)
	if source_note:
		conversation.append_message("System", source_note)
	conversation.insert(ignore_permissions=False)

	return conversation.name


def _thread_source(source_doctype, source_name):
	"""Lets a user start an AI thread from any comment or email on a
	document's timeline. Returns (note, reference_doctype, reference_name, title)."""
	if source_doctype not in THREAD_SOURCE_DOCTYPES:
		frappe.throw(_("Cannot start a Support AI thread from {0}.").format(source_doctype))

	source = frappe.get_doc(source_doctype, source_name)
	reference_doctype, reference_name = source.reference_doctype, source.reference_name
	if reference_doctype and reference_name:
		if not frappe.has_permission(reference_doctype, "read", doc=reference_name):
			frappe.throw(_("You do not have permission to read {0} {1}").format(reference_doctype, reference_name))
	else:
		source.check_permission("read")

	text = strip_html_tags(source.content or "").strip()
	if source_doctype == "Communication":
		author = source.sender_full_name or source.sender
		header = f"an email '{source.subject}' from {author}"
	else:
		author = frappe.utils.get_fullname(source.owner)
		header = f"a comment by {author}"

	location = f" on {reference_doctype} {reference_name}" if reference_doctype else ""
	note = (
		f"The user started this conversation from {header}{location} "
		f"(posted {frappe.utils.format_datetime(source.creation)}). Its text:\n{text[:4000]}"
	)
	title = f"Re: {text[:60]}" if text else f"{source_doctype} {source_name}"
	return note, reference_doctype, reference_name, title


@frappe.whitelist()
def branch_conversation(conversation, message_index):
	"""Starts a new conversation that carries this one's history up to and
	including message_index, so a user can explore a different follow-up
	without losing the original thread."""
	parent = frappe.get_doc("Support AI Conversation", conversation)
	parent.check_permission("read")
	message_index = int(message_index)

	branch = frappe.get_doc(
		{
			"doctype": "Support AI Conversation",
			"title": f"Branch: {parent.title}"[:140],
			"user": frappe.session.user,
			"bot": parent.bot,
			"reference_doctype": parent.reference_doctype,
			"reference_name": parent.reference_name,
			"parent_conversation": parent.name,
		}
	)
	for m in parent.messages[: message_index + 1]:
		branch.append(
			"messages",
			{
				"role": m.role,
				"content": m.content,
				"sent_at": m.sent_at,
				"attachments": m.attachments,
				"attachment_context": m.attachment_context,
				"thinking": m.thinking,
			},
		)
	branch.insert(ignore_permissions=False)
	return branch.name


@frappe.whitelist()
def ask(conversation, message, reference_doctype=None, reference_name=None, attachments=None, list_context=None):
	"""
	Answers one chat message - from the database when it is a plain data
	question, otherwise with the LLM. See support_ai.ai.turn.ChatTurn.

	Endpoint: /api/method/support_ai.api.ask
	Method: POST

	Parameters:
		conversation (str, required): Support AI Conversation name.
		message (str, required): The user's message.
		reference_doctype (str, optional): Document the chat is about.
		reference_name (str, optional): Its name.
		attachments (str | list, optional): JSON list of file_urls.
		list_context (str | dict, optional): JSON {"doctype", "filters"} of the open list view.

	Returns:
		dict: {"conversation", "reply", "thinking", "debug_info", "actions", "answered_by", "message_index"}
	"""
	from support_ai.ai.turn import ChatTurn

	if not message or not message.strip():
		frappe.throw(_("Message cannot be empty."))

	convo = frappe.get_doc("Support AI Conversation", conversation)
	convo.check_permission("write")
	bot = get_bot(convo.bot) if convo.bot else None

	attachments = frappe.parse_json(attachments) if isinstance(attachments, str) and attachments else attachments
	attachments = [a for a in (attachments or []) if isinstance(a, str)]
	list_context = frappe.parse_json(list_context) if isinstance(list_context, str) and list_context else list_context
	if not isinstance(list_context, dict) or not list_context.get("doctype"):
		list_context = None

	turn = ChatTurn(convo, bot, message, reference_doctype, reference_name, attachments, list_context)
	return turn.run()


def build_context_payload(reference_doctype, reference_name, has_tools=True):
	if reference_doctype and reference_name:
		return build_document_context(reference_doctype, reference_name)
	if not has_tools:
		return None
	# No document referenced: don't preload the full custom-apps/doctypes
	# overview here - on a small-context local model that alone can be a
	# large fraction of the available prompt budget, and the tools already
	# let the model query real doctypes/data on demand instead.
	return {"note": "No specific document referenced. Use the available tools for any data question."}


@frappe.whitelist()
def list_conversations():
	return frappe.get_list(
		"Support AI Conversation",
		filters={"user": frappe.session.user},
		fields=["name", "title", "status", "bot", "reference_doctype", "reference_name", "modified"],
		order_by="modified desc",
		limit_page_length=50,
	)


@frappe.whitelist()
def search_conversations(query=""):
	"""Searches the current user's own conversation titles and message
	content. Always scoped to frappe.session.user (same as list_conversations)
	even though this runs as raw SQL, so it can never surface another user's
	chat history."""
	query = (query or "").strip()
	if not query:
		return list_conversations()

	return frappe.db.sql(
		"""
		select c.name, c.title, c.status, c.bot, c.reference_doctype, c.reference_name, c.modified
		from `tabSupport AI Conversation` c
		where c.user = %(user)s
		  and (
		  	c.title like %(like)s
		  	or exists (
		  		select 1 from `tabSupport AI Message` m
		  		where m.parent = c.name and m.content like %(like)s
		  	)
		  )
		order by c.modified desc
		limit 50
		""",
		{"user": frappe.session.user, "like": f"%{query}%"},
		as_dict=True,
	)


@frappe.whitelist()
def get_conversation(name):
	convo = frappe.get_doc("Support AI Conversation", name)
	convo.check_permission("read")
	return convo


@frappe.whitelist()
def edit_message(conversation, keep_count):
	"""Drops every message from index keep_count onward, so a user can edit
	an earlier prompt and get a fresh reply instead of appending after now-
	stale history. The caller is expected to resend the edited text via
	ask() right after this, which appends it as a new message as usual."""
	keep_count = int(keep_count)
	convo = frappe.get_doc("Support AI Conversation", conversation)
	convo.check_permission("write")

	convo.set("messages", convo.messages[:keep_count])
	convo.save(ignore_permissions=False)
	return {"conversation": convo.name}


# --- Saved prompts --------------------------------------------------------------


@frappe.whitelist()
def get_saved_prompts(bot=None):
	"""Global prompts plus the user's own, for this bot or for every bot."""
	prompt = frappe.qb.DocType("Support AI Prompt")
	query = (
		frappe.qb.from_(prompt)
		.select(prompt.name, prompt.prompt, prompt.bot, prompt.is_global, prompt.owner)
		.where((prompt.is_global == 1) | (prompt.owner == frappe.session.user))
		.orderby(prompt.is_global, order=frappe.qb.desc)
		.orderby(prompt.modified, order=frappe.qb.desc)
		.limit(100)
	)
	if bot:
		query = query.where(prompt.bot.isnull() | (prompt.bot == "") | (prompt.bot == bot))
	else:
		query = query.where(prompt.bot.isnull() | (prompt.bot == ""))
	return query.run(as_dict=True)


@frappe.whitelist()
def save_prompt(prompt, bot=None, is_global=0):
	doc = frappe.get_doc(
		{"doctype": "Support AI Prompt", "prompt": prompt, "bot": bot or None, "is_global": frappe.utils.cint(is_global)}
	).insert()
	return doc.name
