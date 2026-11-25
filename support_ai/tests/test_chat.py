# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Tests for one chat turn and the chat's one-click actions: router vs LLM
answers, stored answer metadata, reasoning model choice, rate limit,
feedback, CSV export, suggested questions, confirm-before-write and
streaming. The LLM is always mocked - no model server is needed."""

import json
import time
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from support_ai.ai import chat_actions
from support_ai.ai.llm import LLMConfig, LLMError, chat_message
from support_ai.ai.streaming import (
	_finish_tool_calls,
	_merge_tool_call_deltas,
	stream_ollama,
	stream_openai_style,
)
from support_ai.ai.tools import ToolContext, ToolSpec, run_tool
from support_ai.ai.turn import ChatTurn, _tool_sources, check_rate_limit
from support_ai.api import ask, new_conversation
from support_ai.tests.helpers import (
	TEST_EMAIL,
	actions_of,
	clear_settings_cache,
	make_conversation,
	make_role,
	make_test_user,
	make_todo,
)


def set_settings(**values):
	"""Saves Support AI Settings values inside the test transaction."""
	settings = frappe.get_single("Support AI Settings")
	settings.update(values)
	settings.save(ignore_permissions=True)


def fake_llm(content, tool_debug=None):
	"""A run_builtin stand-in that returns content and records tool calls."""

	def run(config, messages, tools, ctx, max_rounds=2):
		"""Mimics the built-in engine without calling a model."""
		ctx.debug.extend(tool_debug or [])
		return {"content": content, "thinking": None}

	return run


class ChatTestCase(IntegrationTestCase):
	"""Common setup: no rate limit, streaming off, fresh settings."""

	def setUp(self):
		"""Settings that keep tests independent of each other."""
		set_settings(rate_limit_per_minute=0, enable_streaming=0, reasoning_model=None)
		make_test_user()

	def tearDown(self):
		"""Back to Administrator with fresh settings."""
		frappe.set_user("Administrator")
		clear_settings_cache()


class TestChatTurn(ChatTestCase):
	"""support_ai.api.ask / ai.turn.ChatTurn."""

	def test_router_answer_is_stored_with_metadata(self):
		"""Data questions are answered by the router and stored with timing and actions."""
		make_todo("turn test")
		convo = new_conversation(title="t")
		reply = ask(convo, "latest 2 todos")
		self.assertEqual(reply["answered_by"], "Router")
		self.assertEqual(reply["message_index"], 1)
		self.assertEqual({a["type"] for a in reply["actions"]}, {"open_list", "export"})

		row = frappe.get_doc("Support AI Conversation", convo).messages[1]
		self.assertEqual(row.answered_by, "Router")
		self.assertIsNotNone(row.response_time)
		self.assertEqual({a["type"] for a in actions_of(row)}, {"open_list", "export"})

	def test_llm_answer_gets_source_line(self):
		"""Non-data questions go to the model; its tool lookups become a Source line."""
		convo = new_conversation(title="t")
		debug = [{"tool": "lookup_records", "arguments": {"doctype": "ToDo", "filters": {"status": "Open"}}}]
		with patch("support_ai.ai.runner.run_builtin", side_effect=fake_llm("Hello!", debug)):
			reply = ask(convo, "hello there, what can you do")
		self.assertEqual(reply["answered_by"], "LLM")
		self.assertTrue(reply["reply"].startswith("Hello!"))
		self.assertIn('_Source: ToDo {"status": "Open"}_', reply["reply"])

	def test_llm_error_is_stored_as_error(self):
		"""A model failure is shown to the user and recorded as an Error answer."""
		convo = new_conversation(title="t")
		with patch("support_ai.ai.runner.run_builtin", side_effect=LLMError("server down")):
			reply = ask(convo, "hello there")
		self.assertEqual(reply["answered_by"], "Error")
		self.assertIn("server down", reply["reply"])

	def test_list_context_is_used(self):
		"""'how many of these' on a ToDo list counts ToDos."""
		convo = new_conversation(title="t")
		context = json.dumps({"doctype": "ToDo", "filters": []})
		reply = ask(convo, "how many of these", list_context=context)
		self.assertEqual(reply["answered_by"], "Router")
		self.assertIn("ToDo:", reply["reply"])

	def test_empty_message_is_rejected(self):
		"""A blank message raises a validation error."""
		convo = new_conversation(title="t")
		self.assertRaises(frappe.ValidationError, ask, convo, "   ")

	def test_reasoning_model_only_for_diagnosis(self):
		"""'why ...' questions use the reasoning model; data questions keep the main model."""
		set_settings(reasoning_model="big-reasoning-model")
		convo = make_conversation()
		why = ChatTurn(convo, None, "why does this fail")
		why._load_config()
		self.assertEqual(why.config.model, "big-reasoning-model")
		data = ChatTurn(convo, None, "latest todos")
		data._load_config()
		self.assertNotEqual(data.config.model, "big-reasoning-model")

	def test_stream_sink_follows_setting(self):
		"""Streaming can be switched off in Settings."""
		convo = make_conversation()
		self.assertIsNone(ChatTurn(convo, None, "hi")._stream_sink())
		set_settings(enable_streaming=1)
		self.assertIsNotNone(ChatTurn(convo, None, "hi")._stream_sink())

	def test_tool_sources(self):
		"""No tool calls -> no Source line; repeated calls are listed once."""
		self.assertEqual(_tool_sources([]), "")
		calls = [{"tool": "lookup_records", "arguments": {"doctype": "ToDo"}}] * 2
		self.assertEqual(_tool_sources(calls).count("ToDo"), 1)


class TestRateLimit(ChatTestCase):
	"""Per-user questions-per-minute limit."""

	def setUp(self):
		"""Counters live in Redis (not rolled back): start this minute from zero."""
		super().setUp()
		minute = int(time.time() // 60)
		for user in (TEST_EMAIL, "Administrator"):
			for window in (minute, minute + 1):
				frappe.cache.execute_command("DEL", f"support_ai_rate:{frappe.local.site}:{user}:{window}")

	def test_limit_blocks_extra_questions(self):
		"""With a limit of 1, the second question in the same minute is refused."""
		set_settings(rate_limit_per_minute=1)
		frappe.set_user(TEST_EMAIL)
		check_rate_limit()
		self.assertRaises(frappe.ValidationError, check_rate_limit)

	def test_zero_means_no_limit(self):
		"""0 disables the limit."""
		for _ in range(5):
			check_rate_limit()


class TestChatActions(ChatTestCase):
	"""Feedback, export and suggested questions."""

	def test_feedback_on_assistant_answer(self):
		"""👎 with a comment is stored; clearing removes it."""
		convo = make_conversation([{"role": "User", "content": "q"}, {"role": "Assistant", "content": "a"}])
		self.assertEqual(chat_actions.set_feedback(convo.name, 1, "Down", "wrong")["feedback"], "Down")
		row = frappe.get_doc("Support AI Conversation", convo.name).messages[1]
		self.assertEqual((row.feedback, row.feedback_comment), ("Down", "wrong"))
		self.assertIsNone(chat_actions.set_feedback(convo.name, 1, "")["feedback"])

	def test_feedback_validation(self):
		"""Only Up/Down on assistant messages that exist."""
		convo = make_conversation([{"role": "User", "content": "q"}, {"role": "Assistant", "content": "a"}])
		self.assertRaises(frappe.ValidationError, chat_actions.set_feedback, convo.name, 1, "Maybe")
		self.assertRaises(frappe.ValidationError, chat_actions.set_feedback, convo.name, 0, "Up")
		self.assertRaises(frappe.ValidationError, chat_actions.set_feedback, convo.name, 9, "Up")

	def test_export_records(self):
		"""Export returns an ID column first and drops unknown fields."""
		todo = make_todo("export test")
		result = chat_actions.export_records("ToDo", {"name": todo.name}, ["name", "status", "no_such_field"])
		self.assertEqual(result["columns"], ["ID", "Status"])
		self.assertEqual(result["rows"], [[todo.name, "Open"]])

	def test_suggestions_by_role(self):
		"""Settings rows for the user's roles come first; other roles' rows are hidden."""
		hidden_role = make_role("SAI Hidden Suggestion Role")
		settings = frappe.get_single("Support AI Settings")
		settings.set("suggested_prompts", [])
		settings.append("suggested_prompts", {"prompt": "Everyone question"})
		settings.append("suggested_prompts", {"prompt": "Hidden question", "role": hidden_role})
		settings.save(ignore_permissions=True)
		frappe.set_user(TEST_EMAIL)
		suggestions = chat_actions.get_suggestions()
		self.assertEqual(suggestions[0], "Everyone question")
		self.assertNotIn("Hidden question", suggestions)
		self.assertIn("What needs my attention?", suggestions)
		self.assertLessEqual(len(suggestions), 6)


class TestWriteConfirmation(ChatTestCase):
	"""Data changes proposed by the AI wait for the user's Confirm."""

	def setUp(self):
		"""A conversation and a fake write tool that records calls."""
		super().setUp()
		self.convo = make_conversation()
		self.calls = []
		self.spec = ToolSpec(
			"set_todo",
			"Set a ToDo field",
			{},
			lambda args: self.calls.append(args) or {"message": "ok"},
			is_write=True,
		)

	def hold(self):
		"""Runs the write tool unconfirmed; returns (result, pending action)."""
		ctx = ToolContext(conversation=self.convo.name)
		result = run_tool(self.spec, {"document_id": "X"}, ctx)
		return result, ctx.pending_actions[0]

	def test_write_is_held_not_run(self):
		"""An unconfirmed write runs nothing and yields a confirm card."""
		result, action = self.hold()
		self.assertEqual(result["status"], "awaiting_user_confirmation")
		self.assertEqual(action["type"], "confirm_write")
		self.assertEqual(self.calls, [])

	def test_confirmed_context_runs_write(self):
		"""With ctx.confirmed the write runs."""
		run_tool(self.spec, {"document_id": "X"}, ToolContext(conversation=self.convo.name, confirmed=True))
		self.assertEqual(self.calls, [{"document_id": "X"}])

	def test_cancel_records_message_and_expires(self):
		"""Cancel adds a message; the same action cannot be used twice."""
		_result, action = self.hold()
		out = chat_actions.resolve_write_action(action["action_id"], False)
		self.assertIn("nothing was changed", out["reply"])
		self.assertRaises(
			frappe.ValidationError, chat_actions.resolve_write_action, action["action_id"], False
		)

	def test_confirm_unavailable_tool(self):
		"""Confirming a tool the user no longer has does nothing."""
		_result, action = self.hold()
		out = chat_actions.resolve_write_action(action["action_id"], True)
		self.assertIn("no longer available", out["reply"])
		self.assertEqual(self.calls, [])

	def test_other_user_cannot_resolve(self):
		"""Only the user who got the card can confirm it."""
		_result, action = self.hold()
		frappe.set_user(TEST_EMAIL)
		self.assertRaises(
			frappe.ValidationError, chat_actions.resolve_write_action, action["action_id"], True
		)


class FakeStreamResponse:
	"""Stands in for a streaming requests.Response."""

	def __init__(self, lines):
		"""Lines the fake server sends."""
		self.lines = lines

	def raise_for_status(self):
		"""Always a 200."""

	def iter_lines(self):
		"""Yields the lines as bytes, like requests does."""
		yield from (line.encode() for line in self.lines)


class TestStreaming(IntegrationTestCase):
	"""Streaming parsers for Ollama and OpenAI-style servers."""

	def setUp(self):
		"""A config pointing at a fake server."""
		self.config = LLMConfig(provider="Ollama", model="m", base_url="http://fake", timeout=5)

	def test_ollama_stream(self):
		"""Content chunks reach on_token in order; tool calls are collected."""
		lines = [
			json.dumps({"message": {"content": "Hel"}}),
			"",
			json.dumps({"message": {"content": "lo", "thinking": "hmm"}}),
			json.dumps({"message": {"tool_calls": [{"function": {"name": "t", "arguments": {"a": 1}}}]}}),
		]
		tokens = []
		with patch("support_ai.ai.streaming.requests.post", return_value=FakeStreamResponse(lines)):
			result = stream_ollama(self.config, {"model": "m"}, tokens.append)
		self.assertEqual(tokens, ["Hel", "lo"])
		self.assertEqual(result["content"], "Hello")
		self.assertEqual(result["thinking"], "hmm")
		self.assertEqual(result["tool_calls"], [{"name": "t", "arguments": {"a": 1}}])

	def test_openai_stream_joins_tool_call_fragments(self):
		"""SSE deltas are joined; tool-call argument fragments are parsed at the end."""
		delta = lambda d: "data: " + json.dumps({"choices": [{"delta": d}]})
		lines = [
			delta({"content": "Hi"}),
			delta({"tool_calls": [{"index": 0, "function": {"name": "look", "arguments": '{"doc'}}]}),
			delta({"tool_calls": [{"index": 0, "function": {"arguments": 'type": "ToDo"}'}}]}),
			"data: [DONE]",
		]
		tokens = []
		with patch("support_ai.ai.streaming.requests.post", return_value=FakeStreamResponse(lines)):
			result = stream_openai_style(self.config, {}, {"model": "m"}, tokens.append)
		self.assertEqual(tokens, ["Hi"])
		self.assertEqual(result["tool_calls"], [{"name": "look", "arguments": {"doctype": "ToDo"}}])

	def test_bad_tool_arguments_become_empty(self):
		"""Unparseable streamed arguments don't crash the turn."""
		calls = {}
		_merge_tool_call_deltas(calls, [{"index": 0, "function": {"name": "x", "arguments": "{oops"}}])
		self.assertEqual(_finish_tool_calls(calls), [{"name": "x", "arguments": {}}])

	def test_chat_message_streams_when_on_token_given(self):
		"""chat_message hands off to the streaming call only when on_token is set."""
		set_settings(is_enabled=1)
		expected = {"content": "x", "tool_calls": [], "thinking": None}
		with patch("support_ai.ai.streaming.stream_ollama", return_value=expected) as streamed:
			self.assertEqual(
				chat_message([{"role": "user", "content": "hi"}], config=self.config, on_token=print),
				expected,
			)
		streamed.assert_called_once()
		clear_settings_cache()
