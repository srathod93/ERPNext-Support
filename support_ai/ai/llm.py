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
	"""messages: list of {"role": "system"|"user"|"assistant", "content": str}"""
	settings = get_settings()

	if not settings.is_enabled:
		raise LLMError("Support AI is disabled. Enable it in Support AI Settings.")

	temperature = temperature if temperature is not None else (settings.temperature or 0.2)
	timeout = settings.request_timeout or 60
	provider = settings.llm_provider

	if provider == "Ollama":
		return _chat_ollama(settings, messages, temperature, timeout)
	elif provider == "OpenAI Compatible":
		return _chat_openai_compatible(settings, messages, temperature, timeout)

	raise LLMError(f"Unsupported LLM provider: {provider}")


def _chat_ollama(settings, messages, temperature, timeout):
	base_url = (settings.ollama_base_url or "http://localhost:11434").rstrip("/")
	model = settings.ollama_model or "llama3.1"

	try:
		response = requests.post(
			f"{base_url}/api/chat",
			json={
				"model": model,
				"messages": messages,
				"stream": False,
				"options": {"temperature": temperature},
			},
			timeout=timeout,
		)
		response.raise_for_status()
	except requests.RequestException as e:
		frappe.log_error(title="Support AI: Ollama request failed", message=frappe.get_traceback())
		raise LLMError(
			f"Could not reach Ollama at {base_url}. Is `ollama serve` running and is the "
			f"model `{model}` pulled? ({e})"
		)

	data = response.json()
	content = (data.get("message") or {}).get("content")
	if not content:
		raise LLMError("Ollama returned an empty response.")
	return content


def _chat_openai_compatible(settings, messages, temperature, timeout):
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

	try:
		response = requests.post(
			f"{base_url}/chat/completions",
			headers=headers,
			json={"model": model, "messages": messages, "temperature": temperature},
			timeout=timeout,
		)
		response.raise_for_status()
	except requests.RequestException as e:
		frappe.log_error(title="Support AI: API request failed", message=frappe.get_traceback())
		raise LLMError(f"Could not reach the configured API endpoint. ({e})")

	data = response.json()
	try:
		content = data["choices"][0]["message"]["content"]
	except (KeyError, IndexError):
		raise LLMError("Received an unexpected response shape from the API.")

	if not content:
		raise LLMError("The API returned an empty response.")
	return content
