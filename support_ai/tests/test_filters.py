# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Tests for the router's wording -> filter helpers: date ranges, amounts,
link values and item filters (support_ai.diagnostics.filters)."""

import datetime

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, getdate, nowdate

from support_ai.diagnostics.filters import (
	detect_amount_filter,
	detect_date_range,
	detect_item_filter,
	detect_link_filters,
	match_month,
	search_link,
	value_words,
)
from support_ai.diagnostics.vocabulary import STOP_WORDS, tokens_of
from support_ai.tests.helpers import HAS_ERPNEXT, make_role


def date_range(text):
	"""(start, end) the helper finds in text, or None."""
	found, _label, _used = detect_date_range(tokens_of(text), text, STOP_WORDS)
	return found


class TestDateRanges(IntegrationTestCase):
	"""Relative and explicit date wording."""

	def setUp(self):
		"""Today's date, for relative ranges."""
		self.today = getdate(nowdate())

	def test_today_and_yesterday(self):
		"""'today' and 'yesterday' are single days."""
		self.assertEqual(date_range("todos today"), (self.today, self.today))
		yesterday = self.today - datetime.timedelta(days=1)
		self.assertEqual(date_range("todos yesterday"), (yesterday, yesterday))

	def test_last_n_days_weeks_months(self):
		"""'last 7 days' covers 7 days; 'last 3 months' starts 3 months back."""
		self.assertEqual(date_range("invoices last 7 days"), (getdate(add_days(self.today, -6)), self.today))
		self.assertEqual(date_range("past 2 weeks")[0], getdate(add_days(self.today, -13)))
		self.assertEqual(date_range("last 3 months")[0], getdate(add_months(self.today, -3)))

	def test_this_and_last_week(self):
		"""Weeks start on Monday; last week is 7 full days."""
		start, end = date_range("orders this week")
		self.assertEqual(start.weekday(), 0)
		self.assertEqual(end, self.today)
		start, end = date_range("orders last week")
		self.assertEqual((start.weekday(), (end - start).days), (0, 6))

	def test_this_and_last_month(self):
		"""'this month' starts on the 1st; 'last month' is the whole previous month."""
		self.assertEqual(date_range("this month")[0], self.today.replace(day=1))
		start, end = date_range("last month")
		self.assertEqual(start.day, 1)
		self.assertEqual(end, self.today.replace(day=1) - datetime.timedelta(days=1))

	def test_quarters(self):
		"""Quarters start on Jan/Apr/Jul/Oct 1; last quarter ends the day before this one."""
		this_start, _end = date_range("this quarter")
		self.assertIn(this_start.month, (1, 4, 7, 10))
		last_start, last_end = date_range("last quarter")
		self.assertEqual(last_end, this_start - datetime.timedelta(days=1))
		self.assertEqual(last_start.day, 1)

	def test_years(self):
		"""'this year', 'last year' and a bare year."""
		self.assertEqual(date_range("this year")[0], datetime.date(self.today.year, 1, 1))
		self.assertEqual(date_range("last year")[1], datetime.date(self.today.year - 1, 12, 31))
		self.assertEqual(
			date_range("invoices of 2025"), (datetime.date(2025, 1, 1), datetime.date(2025, 12, 31))
		)

	def test_month_names_and_typos(self):
		"""'march 2026' is that month; 'febuary' is February."""
		self.assertEqual(date_range("march 2026"), (datetime.date(2026, 3, 1), datetime.date(2026, 3, 31)))
		self.assertEqual(match_month("febuary"), 2)
		self.assertIsNone(match_month("many"))

	def test_explicit_dates(self):
		"""'between X and Y', 'since X', 'before X', 'on X', in several formats."""
		self.assertEqual(
			date_range("between 1 sep 2026 and 15 sep 2026"),
			(datetime.date(2026, 9, 1), datetime.date(2026, 9, 15)),
		)
		self.assertEqual(
			date_range("from 2026-01-15 to 2026-02-01"),
			(datetime.date(2026, 1, 15), datetime.date(2026, 2, 1)),
		)
		self.assertEqual(date_range("since 15/01/2026"), (datetime.date(2026, 1, 15), self.today))
		self.assertEqual(date_range("before 15 jan 2026")[1], datetime.date(2026, 1, 15))
		self.assertEqual(
			date_range("on 15 jan 2026"), (datetime.date(2026, 1, 15), datetime.date(2026, 1, 15))
		)

	def test_financial_year(self):
		"""'this financial year' uses the ERPNext Fiscal Year."""
		if not HAS_ERPNEXT:
			self.skipTest("needs ERPNext")
		found, label, _used = detect_date_range(tokens_of("this financial year"))
		if not found:
			self.skipTest("no Fiscal Year covers today on this site")
		self.assertTrue(found[0] <= self.today <= found[1])
		self.assertIn("financial year", label)

	def test_no_dates(self):
		"""Text without date words gives no range."""
		self.assertIsNone(date_range("show open todos"))


class TestAmounts(IntegrationTestCase):
	"""'above 1 lakh' style amounts."""

	def setUp(self):
		"""Purchase Invoice meta (has grand_total) when ERPNext is installed."""
		if not HAS_ERPNEXT:
			self.skipTest("needs ERPNext")
		self.meta = frappe.get_meta("Purchase Invoice")

	def amount(self, text):
		"""(field, condition) found in text, or None."""
		found = detect_amount_filter(self.meta, text)
		return found[:2] if found else None

	def test_units_and_operators(self):
		"""Indian and western units, and each comparison word."""
		self.assertEqual(self.amount("above 1 lakh"), ("grand_total", [">", 100000]))
		self.assertEqual(self.amount("less than 50k"), ("grand_total", ["<", 50000]))
		self.assertEqual(self.amount("at least 2 crore"), ("grand_total", [">=", 20000000]))
		self.assertEqual(self.amount("up to 1,500"), ("grand_total", ["<=", 1500]))
		self.assertEqual(self.amount("over ₹ 2.5 lakhs"), ("grand_total", [">", 250000]))

	def test_no_amount(self):
		"""No comparison word, no filter."""
		self.assertIsNone(self.amount("latest purchase invoices"))

	def test_doctype_without_amount_field(self):
		"""A DocType with no amount field never gets an amount filter."""
		self.assertIsNone(detect_amount_filter(frappe.get_meta("ToDo"), "todos above 1 lakh"))


class TestLinkAndItemFilters(IntegrationTestCase):
	"""'for role X' / 'containing item X' style values."""

	def setUp(self):
		"""A role to search for."""
		self.role = make_role("SAI Filter Test Role")

	def test_value_words_stop_at_stop_words(self):
		"""Value words end at the first stop word."""
		tokens = tokens_of("for role sai filter test role this month")
		self.assertEqual(value_words(tokens, 2, STOP_WORDS), ["sai", "filter", "test", "role"])

	def test_search_link_finds_by_name(self):
		"""Words match a linked record's name (permissions apply)."""
		self.assertIn(self.role, search_link("Role", ["sai", "filter", "test"]))
		self.assertEqual(search_link("Role", []), [])

	def test_link_filter_on_doctype(self):
		"""'todos for role sai filter test' filters ToDo.role."""
		tokens = tokens_of("todos for role sai filter test")
		filters, labels, used, errors = detect_link_filters(frappe.get_meta("ToDo"), tokens, STOP_WORDS)
		self.assertEqual(filters.get("role"), ["in", [self.role]])
		self.assertTrue(labels and not errors)
		self.assertIn("sai", used)

	def test_link_filter_unknown_value(self):
		"""A value that matches nothing is reported, not silently dropped."""
		tokens = tokens_of("todos for role zzqnothing")
		filters, _labels, _used, errors = detect_link_filters(frappe.get_meta("ToDo"), tokens, STOP_WORDS)
		self.assertEqual(filters, {})
		self.assertIn("No Role matching 'zzqnothing'", errors[0])

	def test_item_filter(self):
		"""'containing item X' becomes a child-table filter; unknown items match nothing."""
		if not HAS_ERPNEXT:
			self.skipTest("needs ERPNext")
		meta = frappe.get_meta("Purchase Invoice")
		child_filters, _label, used = detect_item_filter(
			meta, tokens_of("invoices containing item zzqnoitem"), STOP_WORDS
		)
		self.assertEqual(child_filters[0][0], "Purchase Invoice Item")
		self.assertEqual(child_filters[0][2:], ["=", "__no_such_item__"])
		self.assertIn("zzqnoitem", used)
		self.assertIsNone(detect_item_filter(frappe.get_meta("ToDo"), tokens_of("todo item x"), STOP_WORDS))
