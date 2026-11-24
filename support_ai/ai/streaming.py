# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Streaming variants of the Ollama and OpenAI-style chat calls. Each text
chunk is passed to on_token as it arrives (the chat shows the reply word by
word), and the full message is returned in the same shape as the
non-streaming calls in llm.py: {"content", "tool_calls", "thinking"}."""

import json

import frappe
import requests

from support_ai.ai.llm import LLMError


def stream_ollama(config, payload, on_token):
	"""Streams Ollama /api/chat (newline-delimited JSON).

	Parameters:
		config (LLMConfig, required): Resolved provider config.
		payload (dict, required): Request body; "stream" is forced on.
		on_token (callable, required): Called with each content chunk.

	Returns:
		dict: {"content": str, "tool_calls": list, "thinking": str|None}
	"""
	payload = {**payload, "stream": True}
	response = _post(
		f"{config.base_url}/api/chat",
		payload,
		config,
		headers=config.headers,
		auth=config.auth,
		error_hint=f"Is `ollama serve` running and is the model `{config.model}` pulled?",
	)
	content, thinking, tool_calls = [], [], []
	for line in response.iter_lines():
		if not line:
			continue
		message = (json.loads(line).get("message")) or {}
		if message.get("content"):
			content.append(message["content"])
			on_token(message["content"])
		if message.get("thinking"):
			thinking.append(message["thinking"])
		for tc in message.get("tool_calls") or []:
			fn = tc.get("function") or {}
			tool_calls.append({"name": fn.get("name"), "arguments": fn.get("arguments") or {}})
	return {"content": "".join(content), "tool_calls": tool_calls, "thinking": "".join(thinking) or None}


def stream_openai_style(config, headers, payload, on_token):
	"""Streams an OpenAI-compatible /chat/completions call (server-sent events).
	Tool-call arguments arrive as string fragments per index and are joined.

	Parameters:
		config (LLMConfig, required): Resolved provider config.
		headers (dict, required): Request headers.
		payload (dict, required): Request body; "stream" is forced on.
		on_token (callable, required): Called with each content chunk.

	Returns:
		dict: {"content": str, "tool_calls": list, "thinking": str|None}
	"""
	payload = {**payload, "stream": True}
	response = _post(f"{config.base_url}/chat/completions", payload, config, headers=headers)
	content, thinking, calls = [], [], {}
	for raw in response.iter_lines():
		line = raw.decode() if isinstance(raw, bytes) else raw
		if not line.startswith("data:") or line.strip() == "data: [DONE]":
			continue
		choices = json.loads(line[5:].strip()).get("choices") or [{}]
		delta = choices[0].get("delta") or {}
		if delta.get("content"):
			content.append(delta["content"])
			on_token(delta["content"])
		if delta.get("reasoning_content") or delta.get("reasoning"):
			thinking.append(delta.get("reasoning_content") or delta.get("reasoning"))
		_merge_tool_call_deltas(calls, delta.get("tool_calls") or [])
	return {
		"content": "".join(content),
		"tool_calls": _finish_tool_calls(calls),
		"thinking": "".join(thinking) or None,
	}


def _merge_tool_call_deltas(calls, deltas):
	"""Accumulates streamed tool-call fragments into calls[index]."""
	for tc in deltas:
		slot = calls.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
		fn = tc.get("function") or {}
		slot["name"] += fn.get("name") or ""
		slot["arguments"] += fn.get("arguments") or ""


def _finish_tool_calls(calls):
	"""Parses the joined argument strings of streamed tool calls."""
	out = []
	for _index, call in sorted(calls.items()):
		try:
			args = frappe.parse_json(call["arguments"]) if call["arguments"] else {}
		except Exception:
			args = {}
		out.append({"name": call["name"], "arguments": args or {}})
	return out


def _post(url, payload, config, headers=None, auth=None, error_hint=""):
	"""POSTs a streaming request, turning transport errors into LLMError."""
	try:
		response = requests.post(
			url, json=payload, headers=headers, auth=auth, timeout=config.timeout, stream=True
		)
		response.raise_for_status()
		return response
	except requests.RequestException as e:
		frappe.log_error(
			title=f"Support AI: {config.provider} streaming request failed", message=frappe.get_traceback()
		)
		raise LLMError(
			f"Could not get a reply from {config.provider} at {config.base_url}. {error_hint} ({e})".strip()
		) from e
