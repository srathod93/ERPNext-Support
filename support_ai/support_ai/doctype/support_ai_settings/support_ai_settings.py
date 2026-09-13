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
- You are not a data lookup or reporting tool. If asked to fetch, list, count, or find records (e.g. "latest \
Journal Entry ID", "how many invoices this month"), say plainly that this is outside what you do, and point \
the user to the relevant List View (sorted/filtered) or Report Builder instead of fabricating an answer.
- Be specific: cite exact field names, roles, workflow states, or app/module names from the given context rather than generic advice.
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


@frappe.whitelist()
def test_connection():
	"""Send a trivial ping to the configured LLM provider and return its reply."""
	frappe.only_for("System Manager")
	from support_ai.ai.llm import LLMError, chat

	try:
		reply = chat([{"role": "user", "content": "Reply with exactly: OK"}])
	except LLMError as e:
		return {"success": False, "message": str(e)}

	return {"success": True, "reply": reply}
