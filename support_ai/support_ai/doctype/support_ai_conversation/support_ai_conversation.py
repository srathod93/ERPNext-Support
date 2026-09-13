# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class SupportAIConversation(Document):
	def append_message(self, role, content):
		self.append("messages", {"role": role, "content": content, "sent_at": now_datetime()})

	def as_chat_history(self, limit=20):
		role_map = {"User": "user", "Assistant": "assistant", "System": "system"}
		history = [
			{"role": role_map.get(m.role, "user"), "content": m.content} for m in self.messages
		]
		return history[-limit:]
