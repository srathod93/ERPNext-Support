# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Pluggable LLM client. Supports a local, free, self-hosted Ollama server
(default), OpenAI, local LM Studio / LocalAI servers, and any hosted API that
exposes an OpenAI-compatible `/chat/completions` endpoint (e.g. Groq,
OpenRouter). Every setting can be overridden per Support AI Bot."""

from dataclasses import dataclass, field

import frappe
import requests

OPENAI_BASE_URL = "https://api.openai.com/v1"

# Providers that speak the OpenAI /chat/completions dialect.
OPENAI_STYLE_PROVIDERS = ("OpenAI", "OpenAI Compatible", "LM Studio", "LocalAI")


class LLMError(Exception):
	pass


@dataclass
class LLMConfig:
	provider: str
	model: str
	base_url: str
	temperature: float = 0.2
	top_p: float | None = None
	max_output_tokens: int = 500
	timeout: int = 60
	reasoning_effort: str | None = None
	api_key: str | None = None
	headers: dict = field(default_factory=dict)
	auth: tuple | None = None
	organization: str | None = None
	project: str | None = None

	@property
	def supports_hosted_tools(self):
		"""Code Interpreter / Web Search / vector-store File Search only exist
		on OpenAI's own Responses API."""
		return self.provider == "OpenAI"


def get_settings():
	return frappe.get_cached_doc("Support AI Settings")


def get_llm_config(bot=None):
	"""Resolves the effective provider/model/sampling config: bot overrides
	first, then Support AI Settings, then hardcoded defaults."""
	settings = get_settings()
	provider = (bot and bot.provider) or settings.llm_provider or "Ollama"

	def pick(bot_field, settings_value, default):
		value = bot.get(bot_field) if bot else None
		if value not in (None, "", 0):
			return value
		return settings_value if settings_value not in (None, "", 0) else default

	config = LLMConfig(
		provider=provider,
		model="",
		base_url="",
		temperature=pick("temperature", settings.temperature, 0.2),
		top_p=(bot.top_p if bot and bot.top_p else None),
		max_output_tokens=pick("max_output_tokens", settings.max_output_tokens, 500),
		timeout=settings.request_timeout or 60,
		reasoning_effort=(bot.reasoning_effort if bot and bot.reasoning_effort else None),
	)

	if provider == "Ollama":
		config.base_url = (settings.ollama_base_url or "http://localhost:11434").rstrip("/")
		config.model = settings.ollama_model or "llama3.1"
		config.headers, config.auth = _ollama_auth(settings)
		if config.headers.get("Authorization"):
			config.api_key = config.headers["Authorization"].split(" ", 1)[1]
	elif provider == "OpenAI":
		config.base_url = OPENAI_BASE_URL
		config.model = settings.get("openai_model") or "gpt-4o-mini"
		config.api_key = settings.get_password("openai_api_key", raise_exception=False)
		config.organization = settings.get("openai_organisation_id") or None
		config.project = settings.get("openai_project_id") or None
		if not config.api_key:
			raise LLMError("OpenAI API Key is not configured in Support AI Settings.")
		config.headers = {"Authorization": f"Bearer {config.api_key}"}
		if config.organization:
			config.headers["OpenAI-Organization"] = config.organization
		if config.project:
			config.headers["OpenAI-Project"] = config.project
	elif provider == "OpenAI Compatible":
		config.base_url = (settings.api_base_url or "").rstrip("/")
		config.model = settings.api_model or ""
		config.api_key = settings.get_password("api_key", raise_exception=False)
		if not config.base_url:
			raise LLMError("API Base URL is not configured in Support AI Settings.")
	elif provider == "LM Studio":
		config.base_url = (settings.get("lm_studio_base_url") or "http://localhost:1234/v1").rstrip("/")
		config.model = settings.get("lm_studio_model") or ""
	elif provider == "LocalAI":
		config.base_url = (settings.get("localai_base_url") or "http://localhost:8080/v1").rstrip("/")
		config.model = settings.get("localai_model") or ""
		config.api_key = settings.get_password("localai_api_key", raise_exception=False)
	else:
		raise LLMError(f"Unsupported LLM provider: {provider}")

	if provider != "OpenAI" and config.api_key and provider != "Ollama":
		config.headers = {"Authorization": f"Bearer {config.api_key}"}

	if bot and bot.model:
		config.model = bot.model
	if not config.model:
		raise LLMError(f"No model is configured for {provider}. Set it in Support AI Settings or on the bot.")

	return config


def chat(messages, temperature=None, config=None):
	"""messages: list of {"role": "system"|"user"|"assistant", "content": str}
	Returns the reply text. Use chat_message() instead if you passed tools."""
	message = chat_message(messages, temperature=temperature, config=config)
	content = message.get("content")
	if not content:
		raise LLMError("The model returned an empty response.")
	return content


def chat_message(messages, tools=None, temperature=None, config=None, on_token=None):
	"""Returns the raw assistant message as {"content": str|None, "tool_calls":
	[{"name": str, "arguments": dict}, ...], "thinking": str|None}. tool_calls
	is always a list (empty if the model didn't call a tool, which is the
	common case). With on_token, the reply is streamed and each text chunk is
	passed to on_token as it arrives.

	A message may carry "images": [{"mime": "image/png", "data": <base64>}];
	it's converted to each provider's own image format here."""
	settings = get_settings()
	if not settings.is_enabled:
		raise LLMError("Support AI is disabled. Enable it in Support AI Settings.")

	config = config or get_llm_config()
	if temperature is not None:
		config.temperature = temperature

	if config.provider == "Ollama":
		return _chat_ollama(config, messages, tools, on_token)
	if config.provider in OPENAI_STYLE_PROVIDERS:
		return _chat_openai_style(config, messages, tools, on_token)

	raise LLMError(f"Unsupported LLM provider: {config.provider}")


def _chat_ollama(config, messages, tools, on_token=None):
	"""Calls Ollama /api/chat; streams when on_token is given."""
	payload = {
		"model": config.model,
		"messages": [_ollama_message(m) for m in messages],
		"stream": False,
		# num_predict hard-caps generated tokens - without it a small local model
		# can ramble for hundreds of extra tokens instead of calling a tool or
		# giving a concise answer, and on CPU-only hardware each of those tokens
		# is real, slow wall-clock time. Uncapped, this was observed to run past
		# a 240s timeout generating 800+ tokens of rambling on a single reply.
		"options": {"temperature": config.temperature, "num_predict": config.max_output_tokens},
	}
	if config.top_p:
		payload["options"]["top_p"] = config.top_p
	if tools:
		payload["tools"] = tools
	if on_token:
		from support_ai.ai.streaming import stream_ollama

		return stream_ollama(config, payload, on_token)

	try:
		response = requests.post(
			f"{config.base_url}/api/chat",
			json=payload,
			headers=config.headers,
			auth=config.auth,
			timeout=config.timeout,
		)
		response.raise_for_status()
	except requests.RequestException as e:
		frappe.log_error(title="Support AI: Ollama request failed", message=frappe.get_traceback())
		raise LLMError(
			f"Could not reach Ollama at {config.base_url}. Is `ollama serve` running and is the "
			f"model `{config.model}` pulled? ({e})"
		)

	data = response.json()
	message = data.get("message") or {}

	tool_calls = []
	for tc in message.get("tool_calls") or []:
		fn = tc.get("function") or {}
		# Ollama already gives arguments as a dict.
		tool_calls.append({"name": fn.get("name"), "arguments": fn.get("arguments") or {}})

	return {"content": message.get("content"), "tool_calls": tool_calls, "thinking": message.get("thinking")}


def _ollama_message(message):
	out = {"role": message["role"], "content": message.get("content") or ""}
	if message.get("images"):
		out["images"] = [img["data"] for img in message["images"]]
	return out


def _openai_message(message):
	if not message.get("images"):
		return {"role": message["role"], "content": message.get("content") or ""}
	parts = [{"type": "text", "text": message.get("content") or ""}]
	for img in message["images"]:
		parts.append({"type": "image_url", "image_url": {"url": f"data:{img['mime']};base64,{img['data']}"}})
	return {"role": message["role"], "content": parts}


def _ollama_auth(settings):
	"""Returns (headers, auth) for a remote/proxied Ollama server. A plain local
	`ollama serve` needs neither, so auth type "None" sends nothing."""
	auth_type = settings.ollama_auth_type or "None"

	if auth_type == "Bearer Token":
		api_key = settings.get_password("ollama_api_key", raise_exception=False)
		if not api_key:
			raise LLMError("Ollama API Key is not configured in Support AI Settings.")
		return {"Authorization": f"Bearer {api_key}"}, None

	if auth_type == "Basic Auth":
		username = settings.ollama_username
		password = settings.get_password("ollama_password", raise_exception=False)
		if not username or not password:
			raise LLMError("Ollama Username/Password is not configured in Support AI Settings.")
		return {}, (username, password)

	return {}, None


def _chat_openai_style(config, messages, tools, on_token=None):
	"""Calls an OpenAI-compatible /chat/completions; streams when on_token is given."""
	headers = {"Content-Type": "application/json", **config.headers}

	payload = {"model": config.model, "messages": [_openai_message(m) for m in messages]}
	if config.provider == "OpenAI":
		# OpenAI deprecated max_tokens in favour of max_completion_tokens, and
		# reasoning models reject temperature/top_p entirely.
		payload["max_completion_tokens"] = config.max_output_tokens
	else:
		payload["max_tokens"] = config.max_output_tokens
	if config.reasoning_effort:
		payload["reasoning_effort"] = config.reasoning_effort
	else:
		payload["temperature"] = config.temperature
		if config.top_p:
			payload["top_p"] = config.top_p
	if tools:
		payload["tools"] = tools
	if on_token:
		from support_ai.ai.streaming import stream_openai_style

		return stream_openai_style(config, headers, payload, on_token)

	try:
		response = requests.post(
			f"{config.base_url}/chat/completions", headers=headers, json=payload, timeout=config.timeout
		)
		response.raise_for_status()
	except requests.RequestException as e:
		frappe.log_error(title=f"Support AI: {config.provider} request failed", message=frappe.get_traceback())
		if e.response is not None and e.response.status_code in (401, 403):
			if not config.api_key:
				raise LLMError(f"No API Key is set for {config.provider} in Support AI Settings.")
			raise LLMError(
				f"{config.provider} rejected the API Key in Support AI Settings (invalid, revoked, or for a "
				f"different provider). ({e})"
			)
		detail = ""
		if e.response is not None:
			detail = (e.response.text or "")[:300]
		raise LLMError(f"Could not get a reply from {config.provider} at {config.base_url}. ({e}) {detail}".strip())

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

	# LM Studio / DeepSeek-style servers return reasoning separately.
	thinking = message.get("reasoning_content") or message.get("reasoning")
	return {"content": message.get("content"), "tool_calls": tool_calls, "thinking": thinking}


def list_models(config):
	"""Model ids available on the configured server - Ollama's /api/tags or
	the OpenAI-style /models endpoint."""
	try:
		if config.provider == "Ollama":
			response = requests.get(
				f"{config.base_url}/api/tags", headers=config.headers, auth=config.auth, timeout=15
			)
			response.raise_for_status()
			return sorted(m.get("name") for m in response.json().get("models", []) if m.get("name"))

		response = requests.get(f"{config.base_url}/models", headers=config.headers, timeout=15)
		response.raise_for_status()
		return sorted(m.get("id") for m in response.json().get("data", []) if m.get("id"))
	except requests.RequestException as e:
		raise LLMError(f"Could not list models from {config.provider} at {config.base_url}. ({e})")
