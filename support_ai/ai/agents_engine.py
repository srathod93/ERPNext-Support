# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""The OpenAI Agents SDK engine. Works with OpenAI (Responses API, plus the
hosted File Search / Code Interpreter / Web Search tools) and with any
OpenAI-compatible server - Ollama, LM Studio, LocalAI, Groq - through
/chat/completions."""

import asyncio
import json

import frappe

from support_ai.ai.llm import OPENAI_BASE_URL, LLMError
from support_ai.ai.tools import result_text, run_tool


def _client(config):
	import base64

	from openai import AsyncOpenAI

	if config.provider == "Ollama":
		base_url = config.base_url.rstrip("/") + "/v1"
	elif config.provider == "OpenAI":
		base_url = OPENAI_BASE_URL
	else:
		base_url = config.base_url

	# Custom headers override the client's own Bearer header, which is how a
	# Basic-auth protected Ollama proxy is reached.
	headers = {}
	if config.auth:
		token = base64.b64encode(f"{config.auth[0]}:{config.auth[1]}".encode()).decode()
		headers["Authorization"] = f"Basic {token}"
	return AsyncOpenAI(
		api_key=config.api_key or "not-needed",
		base_url=base_url,
		organization=config.organization,
		project=config.project,
		timeout=config.timeout,
		default_headers=headers or None,
	)


def _model_settings(config):
	from agents import ModelSettings

	kwargs = {"max_tokens": config.max_output_tokens}
	if config.reasoning_effort:
		from openai.types.shared import Reasoning

		kwargs["reasoning"] = Reasoning(effort=config.reasoning_effort)
	else:
		kwargs["temperature"] = config.temperature
		if config.top_p:
			kwargs["top_p"] = config.top_p
	return ModelSettings(**kwargs)


def _function_tools(tools, ctx):
	from agents import FunctionTool

	def make(spec):
		async def on_invoke_tool(_run_ctx, args_json):
			args = json.loads(args_json) if args_json else {}
			key = spec.name + json.dumps(args, sort_keys=True, default=str)
			if key in ctx.seen_calls:
				return json.dumps({"note": "Already called with these exact arguments - use the earlier result."})
			ctx.seen_calls.add(key)
			# Called directly on the event-loop thread (not asyncio.to_thread),
			# so frappe.local / the DB connection are the request's own.
			return result_text(run_tool(spec, args, ctx))

		return FunctionTool(
			name=spec.name,
			description=spec.description,
			params_json_schema=spec.parameters,
			on_invoke_tool=on_invoke_tool,
			strict_json_schema=False,
		)

	return [make(spec) for spec in tools]


def _hosted_tools(bot, config):
	if not bot or not config.supports_hosted_tools:
		return []
	from agents import CodeInterpreterTool, FileSearchTool, WebSearchTool

	hosted = []
	if bot.enable_file_search and bot.openai_vector_store_id:
		hosted.append(FileSearchTool(vector_store_ids=[bot.openai_vector_store_id], max_num_results=5))
	if bot.enable_code_interpreter:
		hosted.append(CodeInterpreterTool(tool_config={"type": "code_interpreter", "container": {"type": "auto"}}))
	if bot.enable_web_search:
		hosted.append(WebSearchTool())
	return hosted


def _input_items(llm_messages):
	"""System messages become the agent's instructions; the rest is the
	conversation, with images as input_image parts."""
	instructions, items = [], []
	for m in llm_messages:
		if m["role"] == "system":
			instructions.append(m["content"])
			continue
		if m.get("images"):
			content = [{"type": "input_text", "text": m.get("content") or ""}]
			content += [
				{"type": "input_image", "image_url": f"data:{img['mime']};base64,{img['data']}", "detail": "auto"}
				for img in m["images"]
			]
			items.append({"role": m["role"], "content": content})
		else:
			items.append({"role": m["role"], "content": m.get("content") or ""})
	return "\n\n".join(instructions), items


def run_agents_sdk(config, llm_messages, tools, ctx, bot=None, max_turns=6):
	from agents import Agent, MaxTurnsExceeded, OpenAIProvider, RunConfig, Runner

	instructions, items = _input_items(llm_messages)
	provider = OpenAIProvider(openai_client=_client(config), use_responses=config.provider == "OpenAI")

	agent = Agent(
		name=(bot.name if bot else "Support AI"),
		instructions=instructions,
		model=config.model,
		model_settings=_model_settings(config),
		tools=_function_tools(tools, ctx) + _hosted_tools(bot, config),
	)
	# Tracing would upload every prompt and tool result to OpenAI's trace
	# dashboard - keep conversation data on this server.
	run_config = RunConfig(model_provider=provider, tracing_disabled=True)

	async def _run():
		return await Runner.run(agent, items, max_turns=max_turns, run_config=run_config)

	try:
		result = asyncio.run(_run())
	except MaxTurnsExceeded:
		return {"content": "I couldn't finish this within the allowed number of steps. Please ask a narrower question.", "thinking": None}
	except Exception as e:
		frappe.log_error(title="Support AI: Agents SDK run failed", message=frappe.get_traceback())
		raise LLMError(f"{config.provider} ({config.model}) failed via the Agents SDK: {e}")

	for item in result.new_items:
		if item.type == "tool_call_item":
			raw_type = getattr(item.raw_item, "type", None)
			if raw_type and raw_type != "function_call":
				ctx.debug.append({"tool": raw_type, "hosted": True})

	thinking = []
	for item in result.new_items:
		if item.type == "reasoning_item":
			for summary in getattr(item.raw_item, "summary", None) or []:
				if getattr(summary, "text", None):
					thinking.append(summary.text)

	return {"content": str(result.final_output or "(no response)"), "thinking": "\n\n".join(thinking)}
