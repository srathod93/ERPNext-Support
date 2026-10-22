# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Separates a reasoning model's "thinking" from its actual answer, so the
chat can show the answer cleanly and keep the reasoning behind a toggle."""

import re

THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
# Some local models stop generating (num_predict cap) before closing the tag.
UNCLOSED_THINK = re.compile(r"^\s*<(think|thinking|reasoning)>(.*)$", re.DOTALL | re.IGNORECASE)
BOXED = re.compile(r"\\boxed\{([^}]+)\}")


def extract_thinking(text, provider_thinking=None):
	"""Returns (thinking, answer). provider_thinking is reasoning the API
	already returned in its own field (Ollama `thinking`, LM Studio
	`reasoning_content`), which is merged with any inline <think> blocks."""
	text = text or ""
	parts = [provider_thinking.strip()] if provider_thinking and provider_thinking.strip() else []

	parts.extend(m.group(2).strip() for m in THINK_BLOCK.finditer(text))
	answer = THINK_BLOCK.sub("", text)

	unclosed = UNCLOSED_THINK.match(answer)
	if unclosed:
		parts.append(unclosed.group(2).strip())
		answer = ""

	answer = BOXED.sub(r"\1", answer).strip()
	thinking = "\n\n".join(p for p in parts if p)
	return thinking, answer
