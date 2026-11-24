# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Day-wise Support AI usage: questions asked, how many the database router
answered vs the LLM, errors, average response time, 👍/👎 feedback and
answers that found nothing. Use it to see where the assistant needs work."""

import frappe
from frappe import _
from frappe.query_builder import Case
from frappe.query_builder.functions import Avg, Count, Date, Sum
from frappe.utils import add_days, flt, getdate, nowdate


def execute(filters=None):
	"""Report entry point.

	Parameters:
		filters (dict, optional): {"from_date", "to_date"}.

	Returns:
		tuple: (columns, data, message, chart, report_summary)
	"""
	filters = frappe._dict(filters or {})
	from_date = getdate(filters.from_date or add_days(nowdate(), -30))
	to_date = getdate(filters.to_date or nowdate())
	data = _daily_rows(from_date, to_date)
	return _columns(), data, None, _chart(data), _summary(data)


def _columns():
	"""Report columns."""
	return [
		{"fieldname": "day", "label": _("Date"), "fieldtype": "Date", "width": 110},
		{"fieldname": "answers", "label": _("Answers"), "fieldtype": "Int", "width": 90},
		{"fieldname": "router", "label": _("From Database"), "fieldtype": "Int", "width": 120},
		{"fieldname": "llm", "label": _("From AI Model"), "fieldtype": "Int", "width": 120},
		{"fieldname": "errors", "label": _("Errors"), "fieldtype": "Int", "width": 80},
		{"fieldname": "no_result", "label": _("Nothing Found"), "fieldtype": "Int", "width": 120},
		{"fieldname": "thumbs_up", "label": _("👍"), "fieldtype": "Int", "width": 70},
		{"fieldname": "thumbs_down", "label": _("👎"), "fieldtype": "Int", "width": 70},
		{
			"fieldname": "avg_time",
			"label": _("Avg Time (s)"),
			"fieldtype": "Float",
			"precision": 2,
			"width": 110,
		},
	]


def _daily_rows(from_date, to_date):
	"""One row per day of assistant answers in the range."""
	msg = frappe.qb.DocType("Support AI Message")

	def count_if(condition):
		"""SUM(CASE WHEN condition THEN 1 ELSE 0 END)."""
		return Sum(Case().when(condition, 1).else_(0))

	day = Date(msg.sent_at)
	return (
		frappe.qb.from_(msg)
		.select(
			day.as_("day"),
			Count("*").as_("answers"),
			count_if(msg.answered_by == "Router").as_("router"),
			count_if(msg.answered_by == "LLM").as_("llm"),
			count_if(msg.answered_by == "Error").as_("errors"),
			count_if(msg.no_result == 1).as_("no_result"),
			count_if(msg.feedback == "Up").as_("thumbs_up"),
			count_if(msg.feedback == "Down").as_("thumbs_down"),
			Avg(msg.response_time).as_("avg_time"),
		)
		.where((msg.role == "Assistant") & (msg.parenttype == "Support AI Conversation"))
		.where(day.between(from_date, to_date))
		.groupby(day)
		.orderby(day)
		.run(as_dict=True)
	)


def _chart(data):
	"""Stacked bar chart: database vs AI-model answers per day."""
	if not data:
		return None
	return {
		"data": {
			"labels": [str(r.day) for r in data],
			"datasets": [
				{"name": _("From Database"), "values": [r.router for r in data]},
				{"name": _("From AI Model"), "values": [r.llm for r in data]},
				{"name": _("Errors"), "values": [r.errors for r in data]},
			],
		},
		"type": "bar",
		"barOptions": {"stacked": 1},
	}


def _summary(data):
	"""Totals shown above the report."""
	total = int(sum(r.answers for r in data) or 0)
	router = sum(r.router for r in data)
	down = sum(r.thumbs_down for r in data)
	rated = down + sum(r.thumbs_up for r in data)
	timed = [r for r in data if r.avg_time is not None]
	avg_time = sum(flt(r.avg_time) * r.answers for r in timed) / (sum(r.answers for r in timed) or 1)
	return [
		{"label": _("Answers"), "value": total, "datatype": "Int", "indicator": "Blue"},
		{
			"label": _("Answered from Database"),
			"value": f"{(router * 100 / total) if total else 0:.0f}%",
			"datatype": "Data",
			"indicator": "Green",
		},
		{
			"label": _("Avg Response Time"),
			"value": f"{avg_time:.1f}s",
			"datatype": "Data",
			"indicator": "Blue",
		},
		{
			"label": _("👎 Rate"),
			"value": f"{(down * 100 / rated) if rated else 0:.0f}%",
			"datatype": "Data",
			"indicator": "Red" if down else "Green",
		},
		{
			"label": _("Nothing Found"),
			"value": int(sum(r.no_result or 0 for r in data)),
			"datatype": "Int",
			"indicator": "Orange",
		},
	]
