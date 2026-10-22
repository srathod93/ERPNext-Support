# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

import json
import re

import frappe
from frappe import _
from frappe.model.document import Document

from support_ai.ai.functions import NO_DOCTYPE_TYPES, READ_TYPES, WRITE_TYPES, build_parameters_schema

FUNCTION_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
# Types whose params are fields of the reference DocType.
FIELD_PARAM_TYPES = ("Create Document", "Create Multiple Documents", "Update Document", "Update Multiple Documents", "Get List")


class SupportAIFunction(Document):
	def before_validate(self):
		if self.type in WRITE_TYPES:
			self.requires_write_permissions = 1
		elif self.type in READ_TYPES:
			self.requires_write_permissions = 0

	def validate(self):
		if not FUNCTION_NAME_PATTERN.match(self.function_name or ""):
			frappe.throw(_("Function Name may only contain letters, numbers, '_' and '-' (max 64 characters)."))

		if self.type not in NO_DOCTYPE_TYPES and not self.reference_doctype:
			frappe.throw(_("Please select a Reference DocType for this function."))
		if self.type == "Get Report Result" and not self.report_name:
			frappe.throw(_("Please select a Report."))
		if self.type == "Custom Function":
			self.validate_function_path()

		self.validate_params()
		self.parameters = json.dumps(build_parameters_schema(self), indent=1)

	def validate_function_path(self):
		if not self.function_path:
			frappe.throw(_("Please set the Function Path."))
		try:
			fn = frappe.get_attr(self.function_path)
		except Exception:
			frappe.throw(_("Could not import {0}.").format(self.function_path))
		if fn not in frappe.whitelisted:
			frappe.throw(
				_("{0} must be decorated with @frappe.whitelist() to be used as a Support AI Function.").format(
					self.function_path
				)
			)

	def validate_params(self):
		seen = set()
		for param in self.params:
			if param.fieldname in seen:
				frappe.throw(_("Parameter {0} is listed twice.").format(param.fieldname))
			seen.add(param.fieldname)
			if param.do_not_ask_ai and param.default_value in (None, ""):
				frappe.throw(_("Parameter {0} is hidden from the AI, so it needs a Default Value.").format(param.fieldname))

		if self.type not in FIELD_PARAM_TYPES or not self.reference_doctype:
			return
		meta = frappe.get_meta(self.reference_doctype)
		for param in self.params:
			if param.fieldname in ("name", "owner", "creation", "modified", "docstatus"):
				continue
			if not meta.get_field(param.fieldname):
				frappe.throw(_("Field {0} does not exist in {1}.").format(param.fieldname, self.reference_doctype))
