# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""One question -> one answer. Tries the database router first (exact IDs,
emails, lists, counts, ready-made answers), and only falls back to the LLM
for questions that need reasoning. Also applies the per-user rate limit,
picks the reasoning model for diagnosis questions, streams the model's
reply to the chat, and stores answer metadata (who answered, how long it
took, actions) for the feedback buttons and the usage report."""

import json
import time

import frappe
from frappe import _

from support_ai.ai.formatter import extract_thinking
from support_ai.ai.llm import LLMError, get_llm_config, get_settings
from support_ai.diagnostics.answers import database_facts, run_planned_lookup
from support_ai.diagnostics.formatting import source_line
from support_ai.diagnostics.router import plan_lookup
from support_ai.diagnostics.vocabulary import DIAGNOSTIC_PHRASES, tokens_of


class ChatTurn:
	"""Answers one user message in a conversation."""

	def __init__(
		self,
		convo,
		bot,
		message,
		reference_doctype=None,
		reference_name=None,
		attachments=None,
		list_context=None,
	):
		"""Prepares a turn; nothing is stored until run() is called.

		Parameters:
			convo (Document, required): The Support AI Conversation.
			bot (Document | None, required): The conversation's bot, if any.
			message (str, required): The user's message.
			reference_doctype (str, optional): Document the chat is about.
			reference_name (str, optional): Its name.
			attachments (list[str], optional): file_urls attached to the message.
			list_context (dict, optional): {"doctype", "filters"} of the list view the chat was opened on.
		"""
		self.convo, self.bot, self.message = convo, bot, message
		self.reference_doctype = reference_doctype or convo.reference_doctype
		self.reference_name = reference_name or convo.reference_name
		self.attachments = attachments or []
		self.list_context = list_context
		self.started = time.monotonic()
		self.debug = {"bot": bot.name if bot else None}
		self.config, self.config_error = None, None

	@property
	def has_reference(self):
		"""True when the chat is about one specific document."""
		return bool(self.reference_doctype and self.reference_name)

	def run(self):
		"""Answers the message and returns the reply payload for the client.

		Returns:
			dict: {"conversation", "reply", "thinking", "debug_info", "actions", "answered_by", "message_index"}
		"""
		check_rate_limit()
		self._load_config()
		attachment_context, images = self._prepare_attachments()
		self.convo.append_message(
			"User",
			self.message,
			attachments=json.dumps(self.attachments) if self.attachments else None,
			attachment_context=attachment_context or None,
		)

		if self.config is None:
			return self._reply(
				f"⚠️ Support AI could not get a response: {self.config_error}", answered_by="Error"
			)

		routed = self._try_router()
		if routed:
			return routed
		return self._ask_llm(images)

	def _load_config(self):
		"""Resolves the LLM config; diagnosis questions use the reasoning model."""
		try:
			self.config = get_llm_config(self.bot)
		except LLMError as e:
			self.config_error = str(e)
			return
		reasoning_model = get_settings().get("reasoning_model")
		if reasoning_model and not (self.bot and self.bot.model) and self._needs_reasoning():
			self.config.model = reasoning_model
		engine = self.bot.engine if self.bot else "Built-in"
		self.debug.update({"provider": self.config.provider, "model": self.config.model, "engine": engine})

	def _needs_reasoning(self):
		"""True for "why / how to fix" style questions or a document diagnosis."""
		padded = " " + " ".join(tokens_of(self.message)) + " "
		return self.has_reference or any(f" {p} " in padded for p in DIAGNOSTIC_PHRASES)

	def _prepare_attachments(self):
		"""Extracted text and images of attached files."""
		if not self.attachments:
			return "", []
		from support_ai.ai.files import prepare_attachments

		return prepare_attachments(self.attachments, self.bot, self.config)

	def _try_router(self):
		"""Answers plain data questions straight from the database - small
		local models don't reliably call the lookup tool. None = use the LLM."""
		if not self._router_allowed():
			return None
		earlier = [m.content for m in self.convo.messages[:-1] if m.role == "User"][-5:]
		reference = (self.reference_doctype, self.reference_name) if self.has_reference else None
		plans = plan_lookup(self.message, history=earlier, context=self.list_context, reference=reference)
		if not plans:
			return None
		self.debug["router"] = [p.get("kind") for p in plans]
		result = run_planned_lookup(plans)
		return self._reply(
			result["text"], answered_by="Router", actions=result["actions"], no_result=result["no_result"]
		)

	def _router_allowed(self):
		"""Bots with their own functions decide for themselves; attachments need the model."""
		if self.attachments:
			return False
		return not self.bot or bool(self.bot.include_lookup_tool and not self.bot.functions)

	def _ask_llm(self, images):
		"""Runs the model (with tools) and stores its answer."""
		from support_ai.ai.tools import ToolContext, get_tools

		tools = get_tools(self.bot, self.has_reference, self.config)
		self.debug["tools_offered"] = [t.name for t in tools]
		ctx = ToolContext(bot=self.bot, conversation=self.convo.name, stream=self._stream_sink())
		messages = self._llm_messages(tools, images)

		max_rounds = get_settings().get("max_tool_rounds") or 2
		try:
			result = self._run_engine(messages, tools, ctx, max_rounds)
		except LLMError as e:
			self.debug.update({"error": str(e), "tool_calls": ctx.debug})
			return self._reply(f"⚠️ Support AI could not get a response: {e}", answered_by="Error")

		thinking, reply = extract_thinking(result.get("content"), result.get("thinking"))
		self.debug["tool_calls"] = ctx.debug
		reply = (reply or "(no response)") + _tool_sources(ctx.debug)
		return self._reply(reply, thinking=thinking, answered_by="LLM", actions=ctx.pending_actions)

	def _run_engine(self, messages, tools, ctx, max_rounds):
		"""Built-in tool loop, or the OpenAI Agents SDK when the bot asks for it."""
		if self.bot and self.bot.engine == "OpenAI Agents SDK":
			from support_ai.ai.agents_engine import run_agents_sdk

			return run_agents_sdk(
				self.config, messages, tools, ctx, bot=self.bot, max_turns=max_rounds * 2 + 2
			)
		from support_ai.ai.runner import run_builtin

		return run_builtin(self.config, messages, tools, ctx, max_rounds=max_rounds)

	def _llm_messages(self, tools, images):
		"""System prompt + database facts + diagnostic context + chat history."""
		from support_ai.ai.instructions import build_system_prompt
		from support_ai.api import build_context_payload

		system = build_system_prompt(
			self.bot,
			has_write_tools=any(t.is_write for t in tools),
			reference_doctype=self.reference_doctype,
			reference_name=self.reference_name,
		)
		messages = [{"role": "system", "content": system}]
		# Ground the model in real data for any ID/email the user typed.
		facts = database_facts(self.message)
		if facts:
			messages.append(
				{
					"role": "system",
					"content": "Database lookup for what the user typed (real data):\n" + facts,
				}
			)
		if not self.bot or self.bot.include_diagnostic_context:
			context = build_context_payload(self.reference_doctype, self.reference_name, bool(tools))
			if context is not None:
				messages.append(
					{
						"role": "system",
						"content": "Diagnostic context (read-only, JSON):\n"
						+ json.dumps(context, default=str, indent=2),
					}
				)
		messages.extend(self.convo.as_chat_history())
		if images:
			messages[-1]["images"] = images
		return messages

	def _stream_sink(self):
		"""stream(event, text) that pushes to this user's chat over realtime, or None."""
		if not _streaming_enabled():
			return None
		user, conversation = frappe.session.user, self.convo.name

		def stream(event, text):
			"""Publishes one stream event (token / reset / status) to the user."""
			frappe.publish_realtime(
				"support_ai_stream", {"conversation": conversation, "event": event, "text": text}, user=user
			)

		return stream

	def _reply(self, content, thinking=None, answered_by=None, actions=None, no_result=False):
		"""Stores the assistant message with its metadata and returns the payload."""
		bot = self.bot
		debug_info = json.dumps(self.debug, default=str, indent=1) if bot and bot.debug_mode else None
		elapsed = round(time.monotonic() - self.started, 2)
		response_data = json.dumps({"actions": actions}, default=str) if actions else None
		self.convo.append_message(
			"Assistant",
			content,
			thinking=thinking or None,
			debug_info=debug_info,
			answered_by=answered_by,
			response_time=elapsed,
			no_result=1 if no_result else 0,
			response_data=response_data,
		)
		self.convo.save(ignore_permissions=False)
		return {
			"conversation": self.convo.name,
			"reply": content,
			"thinking": thinking or None,
			"debug_info": debug_info,
			"actions": actions or [],
			"answered_by": answered_by,
			"message_index": len(self.convo.messages) - 1,
		}


def check_rate_limit():
	"""Throws when the user asked more than the per-minute limit in Settings."""
	limit = frappe.utils.cint(get_settings().get("rate_limit_per_minute"))
	if limit <= 0:
		return
	key = f"support_ai_rate:{frappe.local.site}:{frappe.session.user}:{int(time.time() // 60)}"
	count = frappe.cache.incrby(key, 1)
	if count == 1:
		frappe.cache.expire(key, 61)
	if count > limit:
		frappe.throw(
			_(
				"You are asking too fast - the limit is {0} questions per minute. Please wait a moment."
			).format(limit),
			title=_("Slow down"),
		)


def _streaming_enabled():
	"""True unless streaming was switched off in Settings."""
	return bool(frappe.utils.cint(get_settings().get("enable_streaming")))


def _tool_sources(tool_debug):
	"""'Source:' footer listing the data the model looked at, if any."""
	sources = []
	for call in tool_debug or []:
		args = call.get("arguments") or {}
		target = args.get("doctype") or call.get("tool")
		filters = args.get("filters")
		sources.append(f"{target}{' ' + json.dumps(filters, default=str) if filters else ''}")
	if not sources:
		return ""
	return "\n" + source_line(", ".join(dict.fromkeys(sources)))
