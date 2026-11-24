# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Builds the set of tools a conversation may use (built-in lookup, bot
knowledge search, and the bot's configured Support AI Functions) and runs
tool calls with permission re-checks, savepoint rollback on failure, debug
capture and an audit trail for every write."""

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import frappe

from support_ai.ai.functions import build_parameters_schema, check_function_access, run_function

MAX_RESULT_CHARS = 4000


@dataclass
class ToolSpec:
	name: str
	description: str
	parameters: dict
	handler: Callable[[dict], Any]
	is_write: bool = False
	function: str | None = None
	function_type: str | None = None
	reference_doctype: str | None = None

	def openai_schema(self):
		return {
			"type": "function",
			"function": {"name": self.name, "description": self.description, "parameters": self.parameters},
		}


@dataclass
class ToolContext:
	bot: Any = None
	conversation: str | None = None
	debug: list = field(default_factory=list)
	seen_calls: set = field(default_factory=set)
	# stream(event, text): pushes progress/tokens to the chat (None = off).
	stream: Callable | None = None
	# Write calls held for the user's Confirm click (see confirm_write_actions).
	pending_actions: list = field(default_factory=list)
	confirmed: bool = False


def get_tools(bot, has_reference, config):
	"""Tools for one turn. Functions the user isn't allowed to run are left out
	entirely, so the model never sees (or tries) them; run_tool() re-checks
	anyway."""
	from support_ai.ai.knowledge import SEARCH_TOOL_SCHEMA, search_knowledge, uses_openai_vector_store
	from support_ai.diagnostics.lookup import LOOKUP_TOOL_SCHEMA, lookup_records

	tools = []

	# The lookup tool only makes sense for general questions - when a specific
	# document is already referenced we already have its real data in context,
	# and offering the tool there just invites the model to go looking elsewhere.
	if (not bot or bot.include_lookup_tool) and not has_reference:
		fn = LOOKUP_TOOL_SCHEMA["function"]
		tools.append(
			ToolSpec(fn["name"], fn["description"], fn["parameters"], lambda args: lookup_records(**args))
		)

	if bot and bot.enable_file_search and bot.knowledge_files and not uses_openai_vector_store(bot, config):
		tools.append(
			ToolSpec(
				"search_knowledge_base",
				"Search this assistant's knowledge base documents (SOPs, manuals, policies). Use it for "
				"how-to / policy / process questions before answering from general knowledge.",
				SEARCH_TOOL_SCHEMA,
				lambda args, _bot=bot: search_knowledge(_bot, args.get("query") or ""),
			)
		)

	if not bot:
		return tools

	for row in bot.functions or []:
		function_doc = frappe.get_cached_doc("Support AI Function", row.function)
		allowed, _reason = check_function_access(function_doc, bot)
		if not allowed:
			continue
		try:
			parameters = (
				json.loads(function_doc.parameters) if function_doc.parameters else build_parameters_schema(function_doc)
			)
		except Exception:
			frappe.log_error(title=f"Support AI: bad schema for function {function_doc.name}", message=frappe.get_traceback())
			continue

		tools.append(
			ToolSpec(
				name=function_doc.function_name,
				description=function_doc.description,
				parameters=parameters,
				handler=lambda args, _fd=function_doc: run_function(_fd, args),
				is_write=bool(function_doc.requires_write_permissions),
				function=function_doc.name,
				function_type=function_doc.type,
				reference_doctype=function_doc.reference_doctype,
			)
		)

	return tools


def _normalize_args(args):
	if isinstance(args, str):
		try:
			args = frappe.parse_json(args) if args.strip() else {}
		except Exception:
			args = {}
	return args if isinstance(args, dict) else {}


def run_tool(spec, args, ctx):
	"""Executes one tool call and returns a JSON-serialisable result. Never
	raises - errors come back as {"error": ...} so the model can explain them."""
	args = _normalize_args(args)

	if spec.function:
		function_doc = frappe.get_cached_doc("Support AI Function", spec.function)
		allowed, reason = check_function_access(function_doc, ctx.bot)
		if not allowed:
			result = {"error": reason}
			if spec.is_write:
				_log_action(spec, args, ctx, "Denied", error=reason)
			_debug(ctx, spec, args, result)
			return result

	if spec.is_write and not ctx.confirmed:
		result = _hold_for_confirmation(spec, args, ctx)
		_debug(ctx, spec, args, result)
		return result

	savepoint = f"support_ai_tool_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(savepoint)
	try:
		result = spec.handler(args)
		if hasattr(result, "as_dict"):
			result = result.as_dict()
	except Exception as e:
		# Undo whatever the failed call half-wrote (e.g. an insert that failed
		# validation after a child row went in), but keep the rest of the turn.
		frappe.db.rollback(save_point=savepoint)
		frappe.clear_messages()
		error = _error_text(e)
		result = {"error": error}
		if spec.is_write:
			_log_action(spec, args, ctx, "Failed", error=error)
		elif not isinstance(e, (frappe.ValidationError, frappe.PermissionError)):
			frappe.log_error(title=f"Support AI: tool {spec.name} failed", message=frappe.get_traceback())
		_debug(ctx, spec, args, result)
		return result

	if spec.is_write:
		_log_action(spec, args, ctx, "Success", result=result)
	_debug(ctx, spec, args, result)
	return result


def _hold_for_confirmation(spec, args, ctx):
	"""Stores a write call instead of running it; the chat shows a Confirm /
	Cancel card and confirm_write_actions() runs it on Confirm. Held for 30
	minutes, only for the same user and conversation."""
	action_id = frappe.generate_hash(length=12)
	frappe.cache.set_value(
		f"support_ai_pending_action:{action_id}",
		{"user": frappe.session.user, "conversation": ctx.conversation, "tool": spec.name, "args": args},
		expires_in_sec=1800,
	)
	summary = f"{spec.description or spec.name}: {json.dumps(args, default=str)[:300]}"
	ctx.pending_actions.append({"type": "confirm_write", "action_id": action_id, "label": spec.name, "summary": summary})
	return {
		"status": "awaiting_user_confirmation",
		"message": "Nothing has been changed yet. Tell the user what will be done and ask them to press Confirm "
		"under your reply. Do not say it is done.",
	}


def result_text(result):
	text = json.dumps(result, default=str)
	if len(text) > MAX_RESULT_CHARS:
		text = text[:MAX_RESULT_CHARS] + f"... (truncated, {len(text) - MAX_RESULT_CHARS} more chars)"
	return text


def _error_text(e):
	message = str(e) or e.__class__.__name__
	return frappe.utils.strip_html_tags(message)[:500]


def _debug(ctx, spec, args, result):
	text = json.dumps(result, default=str)
	ctx.debug.append(
		{"tool": spec.name, "arguments": args, "result": json.loads(text) if len(text) <= 1500 else text[:1500] + "..."}
	)


def _log_action(spec, args, ctx, status, result=None, error=None):
	reference_name = args.get("document_id")
	if not reference_name and isinstance(result, dict):
		reference_name = result.get("document_id")
	if isinstance(reference_name, (list, dict)):
		reference_name = None

	reference_doctype = spec.reference_doctype or args.get("doctype")
	if not reference_doctype:
		reference_name = None

	try:
		log = frappe.get_doc(
			{
				"doctype": "Support AI Action Log",
				"user": frappe.session.user,
				"status": status,
				"function": spec.function,
				"function_type": spec.function_type,
				"bot": ctx.bot.name if ctx.bot else None,
				"conversation": ctx.conversation,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"arguments": json.dumps(args, default=str, indent=1),
				"result": json.dumps(result, default=str, indent=1)[:10000] if result is not None else None,
				"error": error,
			}
		)
		# A deleted document (or a failed create) has nothing to link to.
		log.flags.ignore_links = True
		log.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Support AI: could not write action log", message=frappe.get_traceback())
