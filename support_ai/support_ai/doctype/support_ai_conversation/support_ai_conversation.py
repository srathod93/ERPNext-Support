# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class SupportAIConversation(Document):
	def append_message(self, role, content, **extra):
		self.append("messages", {"role": role, "content": content, "sent_at": now_datetime(), **extra})

	def as_chat_history(self, limit=20):
		role_map = {"User": "user", "Assistant": "assistant", "System": "system"}
		history = []
		for m in self.messages:
			content = m.content or ""
			# Extracted attachment text is kept on the user message so the file
			# stays in context for follow-up questions, not just the turn it was sent.
			if m.role == "User" and m.attachment_context:
				content = f"{content}\n\n{m.attachment_context}"
			history.append({"role": role_map.get(m.role, "user"), "content": content})

		# Always keep the conversation's starting System note (e.g. the comment
		# a thread was started from) even once it scrolls out of the window.
		if len(history) > limit:
			head = [h for h in history[:1] if h["role"] == "system"]
			history = head + history[-(limit - len(head)) :]
		return history
