# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class SupportAIBot(Document):
	def validate(self):
		if self.instruction_template and not self.instruction:
			template = frappe.get_doc("Support AI Instruction Template", self.instruction_template)
			self.instruction = template.instruction
			self.dynamic_instructions = template.dynamic_instructions

		provider = self.effective_provider()
		if self.enable_code_interpreter or self.enable_web_search:
			if provider != "OpenAI" or self.engine != "OpenAI Agents SDK":
				frappe.throw(_("Code Interpreter and Web Search need Provider = OpenAI and Engine = OpenAI Agents SDK."))

		for row in self.knowledge_files:
			if row.file and not row.file_name:
				row.file_name = frappe.db.get_value("File", {"file_url": row.file}, "file_name") or row.file.rsplit("/", 1)[-1]

		if not self.allow_write_actions and any(row.requires_write_permissions for row in self.functions):
			frappe.msgprint(
				_("Some functions change data but 'Allow Write Actions' is off, so they will not be offered to the AI."),
				indicator="orange",
				alert=True,
			)

	def effective_provider(self):
		return self.provider or frappe.db.get_single_value("Support AI Settings", "llm_provider")

	def on_update(self):
		if (
			self.enable_file_search
			and self.engine == "OpenAI Agents SDK"
			and self.effective_provider() == "OpenAI"
			and self.knowledge_files
		):
			frappe.enqueue(
				"support_ai.ai.knowledge.sync_openai_vector_store",
				bot_name=self.name,
				queue="long",
				enqueue_after_commit=True,
			)


@frappe.whitelist()
def test_bot(bot):
	"""Sends a trivial ping through this bot's provider/model."""
	frappe.only_for("System Manager")
	from support_ai.ai.llm import LLMError, chat, get_llm_config

	try:
		config = get_llm_config(frappe.get_doc("Support AI Bot", bot))
		reply = chat([{"role": "user", "content": "Reply with exactly: OK"}], config=config)
	except LLMError as e:
		return {"success": False, "message": str(e)}
	return {"success": True, "reply": reply, "provider": config.provider, "model": config.model}


@frappe.whitelist()
def sync_knowledge_base(bot):
	frappe.only_for("System Manager")
	from support_ai.ai.knowledge import sync_openai_vector_store

	sync_openai_vector_store(bot)
	return frappe.db.get_value("Support AI Bot", bot, "openai_vector_store_id")
