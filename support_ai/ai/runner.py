# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""The built-in engine: a small, bounded tool-call loop tuned for local
models, on top of support_ai.ai.llm."""

import json

from support_ai.ai.llm import LLMError, chat_message
from support_ai.ai.tools import result_text, run_tool

MAX_TOOL_CONTEXT_CHARS = 4000


def chat_with_image_fallback(messages, tools, config):
	"""Not every local model is vision-capable - if a call carrying images
	fails, retry once without them rather than failing the whole turn."""
	try:
		return chat_message(messages, tools=tools, config=config)
	except LLMError:
		if not any(m.get("images") for m in messages):
			raise
		stripped = [{k: v for k, v in m.items() if k != "images"} for m in messages]
		stripped.append(
			{"role": "system", "content": "Note: the attached image(s) could not be sent - this model cannot read images."}
		)
		return chat_message(stripped, tools=tools, config=config)


def run_builtin(config, llm_messages, tools, ctx, max_rounds=2):
	"""Runs a bounded tool-call loop: the model can call tools more than once
	(e.g. once for GL Entry, once for Journal Entry) before giving its final
	answer. Tool results are fed back as plain messages rather than
	provider-specific tool_call/tool-role replay, which differs enough
	between Ollama and OpenAI-compatible APIs to be a fragile way to get
	what is, in the end, just extra context for the next turn.

	Critically, the message list sent each round is rebuilt from scratch as
	base_messages + a capped tool-context block, rather than appended to -
	on a CPU-only local model, letting tool results accumulate round over
	round can push the prompt past the model's context window, which was
	observed to blow up both memory (~5GB RSS for a 3B model) and latency
	(a single round exceeding 3 minutes) as the runtime pages old context out
	and back in. Capping and rebuilding keeps every round's prompt bounded.

	Returns {"content": str, "thinking": str|None}."""
	by_name = {t.name: t for t in tools}
	tool_schemas = [t.openai_schema() for t in tools] or None

	base_messages = list(llm_messages)
	original_question = next(
		(m.get("content") for m in reversed(llm_messages) if m.get("role") == "user"), None
	)
	all_results = []
	thinking = []

	for _round in range(max(max_rounds, 1)):
		messages = base_messages + _tool_context_messages(all_results, original_question, allow_retry=True)
		assistant_message = chat_with_image_fallback(messages, tool_schemas, config)
		if assistant_message.get("thinking"):
			thinking.append(assistant_message["thinking"])
		tool_calls = assistant_message.get("tool_calls") or []

		if not tool_calls:
			return {"content": assistant_message.get("content") or "(no response)", "thinking": "\n\n".join(thinking)}

		made_new_call = False
		for call in tool_calls:
			spec = by_name.get(call.get("name"))
			if not spec:
				continue

			args = call.get("arguments") or {}
			key = spec.name + json.dumps(args, sort_keys=True, default=str)
			if key in ctx.seen_calls:
				continue
			ctx.seen_calls.add(key)
			made_new_call = True

			all_results.append({"tool": spec.name, "called_with": args, "result": run_tool(spec, args, ctx)})

		if not made_new_call:
			# Model tried to repeat a call (or something unsupported) instead of
			# answering. If we already have real results, force a synthesis of
			# those below rather than returning a content-free filler line.
			break

	if not all_results:
		return {
			"content": "I wasn't able to look that up. Could you rephrase, naming the exact DocType and field you need?",
			"thinking": "\n\n".join(thinking),
		}

	# Force a final answer (tools off) from whatever's been gathered so far,
	# whether we hit the round cap or the model stalled on a repeat call.
	final_messages = base_messages + _tool_context_messages(all_results, original_question, allow_retry=False)
	final = chat_with_image_fallback(final_messages, None, config)
	if final.get("thinking"):
		thinking.append(final["thinking"])
	return {"content": final.get("content") or "(no response)", "thinking": "\n\n".join(thinking)}


def _tool_context_messages(all_results, original_question, allow_retry):
	if not all_results:
		return []

	content = result_text(all_results)
	if len(content) > MAX_TOOL_CONTEXT_CHARS:
		content = (
			content[:MAX_TOOL_CONTEXT_CHARS]
			+ "\n... (truncated - ask a narrower question, e.g. a smaller limit or an extra filter, for more detail)"
		)

	messages = [{"role": "system", "content": "Tool results so far:\n" + content}]
	if original_question:
		instruction = (
			f"Using the tool results above, answer my original question: {original_question}\n"
			"If a result above has an 'error' with a suggested fix, call the tool again with that "
			"correction instead of answering yet."
			if allow_retry
			else f"Using the tool results above, answer my original question now: {original_question}"
		)
		messages.append({"role": "user", "content": instruction})
	return messages
