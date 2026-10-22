# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class SupportAIPrompt(Document):
	def validate(self):
		if self.is_global and "System Manager" not in frappe.get_roles():
			frappe.throw(_("Only a System Manager can create global prompts."))
