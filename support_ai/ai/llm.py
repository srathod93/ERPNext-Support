# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Pluggable LLM client. Supports a local, free, self-hosted Ollama server
(default) and any free-tier hosted API that exposes an OpenAI-compatible
`/chat/completions` endpoint (e.g. Groq, OpenRouter). No paid Anthropic/OpenAI
usage is required to run Support AI."""

import requests
import frappe


class LLMError(Exception):
	pass


def get_settings():
	return frappe.get_cached_doc("Support AI Settings")


def chat(messages, temperature=None):
	"""messages: list of {"role": "system"|"user"|"assistant", "content": str}
	Returns the reply text. Use chat_message() instead if you passed tools."""
	message = chat_message(messages, temperature=temperature)
	content = message.get("content")
	if not content:
		raise LLMError("The model returned an empty response.")
	return content


def chat_message(messages, tools=None, temperature=None):
	"""Returns the raw assistant message as {"content": str|None, "tool_calls":
	[{"name": str, "arguments": dict}, ...]}. tool_calls is always a list
	(empty if the model didn't call a tool, which is the common case)."""
	settings = get_settings()

	if not settings.is_enabled:
		raise LLMError("Support AI is disabled. Enable it in Support AI Settings.")

	temperature = temperature if temperature is not None else (settings.temperature or 0.2)
	timeout = settings.request_timeout or 60
	max_output_tokens = settings.max_output_tokens or 500
	provider = settings.llm_provider

	if provider == "Ollama":
		return _chat_ollama(settings, messages, temperature, timeout, max_output_tokens, tools)
	elif provider == "OpenAI Compatible":
		return _chat_openai_compatible(settings, messages, temperature, timeout, max_output_tokens, tools)

	raise LLMError(f"Unsupported LLM provider: {provider}")


def _chat_ollama(settings, messages, temperature, timeout, max_output_tokens, tools):
	base_url = (settings.ollama_base_url or "http://localhost:11434").rstrip("/")
	model = settings.ollama_model or "llama3.1"

	payload = {
		"model": model,
		"messages": messages,
		"stream": False,
		# num_predict hard-caps generated tokens - without it a small local model
		# can ramble for hundreds of extra tokens instead of calling a tool or
		# giving a concise answer, and on CPU-only hardware each of those tokens
		# is real, slow wall-clock time. Uncapped, this was observed to run past
		# a 240s timeout generating 800+ tokens of rambling on a single reply.
		"options": {"temperature": temperature, "num_predict": max_output_tokens},
	}
	if tools:
		payload["tools"] = tools

	try:
		response = requests.post(f"{base_url}/api/chat", json=payload, timeout=timeout)
		response.raise_for_status()
	except requests.RequestException as e:
		frappe.log_error(title="Support AI: Ollama request failed", message=frappe.get_traceback())
		raise LLMError(
			f"Could not reach Ollama at {base_url}. Is `ollama serve` running and is the "
			f"model `{model}` pulled? ({e})"
		)

	data = response.json()
	message = data.get("message") or {}

	tool_calls = []
	for tc in message.get("tool_calls") or []:
		fn = tc.get("function") or {}
		# Ollama already gives arguments as a dict.
		tool_calls.append({"name": fn.get("name"), "arguments": fn.get("arguments") or {}})

	return {"content": message.get("content"), "tool_calls": tool_calls}


def _chat_openai_compatible(settings, messages, temperature, timeout, max_output_tokens, tools):
	base_url = (settings.api_base_url or "").rstrip("/")
	if not base_url:
		raise LLMError("API Base URL is not configured in Support AI Settings.")

	model = settings.api_model
	if not model:
		raise LLMError("Model Name is not configured in Support AI Settings.")

	api_key = settings.get_password("api_key", raise_exception=False)
	headers = {"Content-Type": "application/json"}
	if api_key:
		headers["Authorization"] = f"Bearer {api_key}"

	payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_output_tokens}
	if tools:
		payload["tools"] = tools

	try:
		response = requests.post(f"{base_url}/chat/completions", headers=headers, json=payload, timeout=timeout)
		response.raise_for_status()
	except requests.RequestException as e:
		frappe.log_error(title="Support AI: API request failed", message=frappe.get_traceback())
		raise LLMError(f"Could not reach the configured API endpoint. ({e})")

	data = response.json()
	try:
		message = data["choices"][0]["message"]
	except (KeyError, IndexError):
		raise LLMError("Received an unexpected response shape from the API.")

	tool_calls = []
	for tc in message.get("tool_calls") or []:
		fn = tc.get("function") or {}
		args = fn.get("arguments")
		# OpenAI-compatible APIs give arguments as a JSON string, not a dict.
		if isinstance(args, str):
			try:
				args = frappe.parse_json(args)
			except Exception:
				args = {}
		tool_calls.append({"name": fn.get("name"), "arguments": args or {}})

	return {"content": message.get("content"), "tool_calls": tool_calls}
