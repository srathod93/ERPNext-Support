# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Builds the system prompt for a conversation: the base diagnostic prompt
from Support AI Settings, plus the bot's own (optionally Jinja-rendered)
instruction."""

import frappe
from frappe.utils import now_datetime, nowdate

WRITE_ACTIONS_ADDENDUM = """
Write actions:
- In this conversation you DO have tools that can change data (create, update, delete, submit, cancel, set values, attach files). The earlier rule that you never perform actions does not apply to those tools.
- Before calling a tool that changes data, make sure the user clearly asked for that change. If anything important is ambiguous (which document, which values), ask first.
- After a write tool runs, report exactly what the tool result says (document IDs, errors). If the result contains an "error", tell the user it failed and why - never claim success.
"""


def get_instruction_variables(reference_doctype=None, reference_name=None):
	user = frappe.get_cached_doc("User", frappe.session.user)

	employee_id = employee_company = department = company = None
	if "erpnext" in frappe.get_installed_apps():
		employee_id = frappe.db.get_value("Employee", {"user_id": frappe.session.user}, "name")
		if employee_id:
			employee_company, department = frappe.db.get_value(
				"Employee", employee_id, ["company", "department"]
			)
		company = frappe.defaults.get_user_default("company") or frappe.db.get_single_value(
			"Global Defaults", "default_company"
		)

	return {
		"first_name": user.first_name,
		"full_name": user.full_name,
		"email": user.email,
		"user_id": frappe.session.user,
		"roles": ", ".join(r for r in frappe.get_roles() if r not in ("All", "Guest")),
		"company": company,
		"employee_id": employee_id,
		"department": department,
		"employee_company": employee_company,
		"lang": (user.language or "en").upper(),
		"today": nowdate(),
		"now": str(now_datetime()),
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
	}


def render_instruction(instruction, dynamic, reference_doctype=None, reference_name=None):
	if not instruction:
		return ""
	if not dynamic:
		return instruction
	# Instructions are written by System Managers only, and frappe's Jinja
	# environment is sandboxed.
	# nosemgrep: frappe-ssti
	return frappe.render_template(
		instruction, get_instruction_variables(reference_doctype, reference_name)
	)


def build_system_prompt(bot=None, has_write_tools=False, reference_doctype=None, reference_name=None):
	from support_ai.support_ai.doctype.support_ai_settings.support_ai_settings import get_system_prompt

	parts = []
	bot_instruction = ""
	if bot:
		bot_instruction = render_instruction(
			bot.instruction, bot.dynamic_instructions, reference_doctype, reference_name
		)

	if not (bot and bot.replace_system_prompt and bot_instruction):
		parts.append(get_system_prompt())
	if bot_instruction:
		parts.append(bot_instruction)
	if has_write_tools:
		parts.append(WRITE_ACTIONS_ADDENDUM.strip())

	return "\n\n".join(parts)


@frappe.whitelist()
def get_instruction_preview(instruction, dynamic_instructions=1):
	"""Renders an instruction for the current user, for the Preview button on
	Support AI Bot / Support AI Instruction Template."""
	frappe.only_for("System Manager")
	return render_instruction(instruction, frappe.utils.cint(dynamic_instructions))
