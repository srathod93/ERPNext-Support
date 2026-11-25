# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Questions Support AI could not answer well: the answer found nothing,
was an error, or got a 👎. Each row shows the user's question next to the
answer, so admins can add a synonym, a suggested question, a knowledge
file, or report a router gap."""

import frappe
from frappe import _
from frappe.utils import add_days, getdate, nowdate

MAX_ROWS = 500


def execute(filters=None):
	"""Report entry point.

	Parameters:
		filters (dict, optional): {"from_date", "to_date"}.

	Returns:
		tuple: (columns, data)
	"""
	filters = frappe._dict(filters or {})
	from_date = getdate(filters.from_date or add_days(nowdate(), -30))
	to_date = getdate(filters.to_date or nowdate())
	return _columns(), _rows(from_date, to_date)


def _columns():
	"""Report columns."""
	return [
		{"fieldname": "sent_at", "label": _("When"), "fieldtype": "Datetime", "width": 160},
		{"fieldname": "user", "label": _("User"), "fieldtype": "Link", "options": "User", "width": 160},
		{"fieldname": "question", "label": _("Question"), "fieldtype": "Data", "width": 320},
		{"fieldname": "answer", "label": _("Answer"), "fieldtype": "Data", "width": 320},
		{"fieldname": "reason", "label": _("Why Listed"), "fieldtype": "Data", "width": 120},
		{"fieldname": "feedback_comment", "label": _("Feedback Comment"), "fieldtype": "Data", "width": 200},
		{
			"fieldname": "conversation",
			"label": _("Conversation"),
			"fieldtype": "Link",
			"options": "Support AI Conversation",
			"width": 140,
		},
	]


def _rows(from_date, to_date):
	"""Weak answers with the user question that came right before each."""
	answer = frappe.qb.DocType("Support AI Message")
	question = frappe.qb.DocType("Support AI Message").as_("question")
	convo = frappe.qb.DocType("Support AI Conversation")
	weak = (answer.no_result == 1) | (answer.feedback == "Down") | (answer.answered_by == "Error")
	rows = (
		frappe.qb.from_(answer)
		.join(convo)
		.on(convo.name == answer.parent)
		.left_join(question)
		.on((question.parent == answer.parent) & (question.idx == answer.idx - 1))
		.select(
			answer.sent_at,
			convo.user,
			convo.name.as_("conversation"),
			question.content.as_("question"),
			answer.content.as_("answer"),
			answer.no_result,
			answer.feedback,
			answer.answered_by,
			answer.feedback_comment,
		)
		.where((answer.role == "Assistant") & (answer.parenttype == "Support AI Conversation") & weak)
		.where(answer.sent_at.between(f"{from_date} 00:00:00", f"{to_date} 23:59:59"))
		.orderby(answer.sent_at, order=frappe.qb.desc)
		.limit(MAX_ROWS)
		.run(as_dict=True)
	)
	for row in rows:
		row.reason = (
			"👎" if row.feedback == "Down" else "Error" if row.answered_by == "Error" else "Nothing found"
		)
		row.answer = (row.answer or "")[:300]
		row.question = (row.question or "")[:300]
	return rows
