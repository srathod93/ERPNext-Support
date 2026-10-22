# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

DEFAULT_SYSTEM_PROMPT = """You are Support AI, an internal diagnostic assistant embedded in this Frappe/ERPNext instance.

Your ONLY job is to analyze the context provided to you (document state, permissions, workflow state, \
validation results, related error logs, and any relevant custom code) and explain to the user:
1. What exactly is wrong.
2. The root cause, referencing the specific fields, roles, doctypes, workflow states, or custom code involved.
3. The precise steps to fix it.

Rules:
- You diagnose and explain only. You never claim to have performed an action, changed data, or fixed anything yourself.
- NEVER invent, guess, or fabricate data (IDs, dates, amounts, names, records) that is not present in the \
context given to you. If the answer isn't in the context, say so plainly and explain what's missing - do not \
produce a plausible-looking example and present it as if it were real.
- For questions asking to find, list, count, or get the latest/recent record(s) of a DocType (e.g. "latest \
Journal Entry ID"), use the lookup_records tool when it is available to you rather than guessing. Only pass \
filters the user actually asked for. If the tool is not available in this conversation, or it returns an \
error or no data, say so plainly and suggest the relevant List View instead - never fabricate an answer.
- Be specific: cite exact field names, roles, workflow states, or app/module names from the given context rather than generic advice.
- Answer in plain English prose. Never paste back the raw JSON context/tool results as your answer - summarize \
the relevant parts of it in words instead.
- If the provided context is insufficient to be sure, say exactly what additional information you need.
- If a fix requires an irreversible or admin-only action (e.g. force-cancel, direct DB edit), clearly flag the risk.
"""


class SupportAISettings(Document):
	def validate(self):
		if not self.system_prompt:
			self.system_prompt = DEFAULT_SYSTEM_PROMPT


def get_system_prompt():
	"""Settings is a Single that may never have been saved, in which case
	its fields read back as empty rather than falling back to the JSON
	default - so callers must go through this instead of reading
	settings.system_prompt directly."""
	settings = frappe.get_cached_doc("Support AI Settings")
	return settings.system_prompt or DEFAULT_SYSTEM_PROMPT


def _config(bot=None):
	from support_ai.ai.llm import get_llm_config

	return get_llm_config(frappe.get_doc("Support AI Bot", bot) if bot else None)


@frappe.whitelist()
def test_connection(bot=None):
	"""Send a trivial ping to the configured LLM provider (or a bot's
	provider/model) and return its reply."""
	frappe.only_for("System Manager")
	from support_ai.ai.llm import LLMError, chat

	try:
		config = _config(bot)
		reply = chat([{"role": "user", "content": "Reply with exactly: OK"}], config=config)
	except LLMError as e:
		return {"success": False, "message": str(e)}

	return {"success": True, "reply": reply, "provider": config.provider, "model": config.model}


@frappe.whitelist()
def get_available_models(provider=None, bot=None):
	"""Lists models on a provider's server. provider overrides the saved
	default so the Settings form can browse any provider it has configured."""
	frappe.only_for("System Manager")
	from support_ai.ai.llm import LLMError, get_llm_config, list_models

	try:
		if provider and not bot:
			# A placeholder model skips the "no model configured" check - listing
			# models is how the admin picks one in the first place.
			stub = frappe._dict(provider=provider, model="-")
			config = get_llm_config(stub)
			config.model = ""
		else:
			config = _config(bot)
		return {"success": True, "provider": config.provider, "models": list_models(config)}
	except LLMError as e:
		return {"success": False, "message": str(e)}
