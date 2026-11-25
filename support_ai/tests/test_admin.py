# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Tests for the admin side of Support AI: the usage and unanswered
questions reports, the daily digest job and the settings defaults set on
migrate."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from support_ai.install import ensure_settings_defaults
from support_ai.support_ai.report.support_ai_unanswered_questions.support_ai_unanswered_questions import (
	execute as unanswered_report,
)
from support_ai.support_ai.report.support_ai_usage.support_ai_usage import execute as usage_report
from support_ai.support_ai.tasks import send_daily_digest
from support_ai.tests.helpers import (
	TEST_EMAIL,
	clear_settings_cache,
	make_conversation,
	make_role,
	make_test_user,
	make_todo,
)

QUESTION = "zzq which report question went wrong"


class TestReports(IntegrationTestCase):
	"""Support AI Usage and Support AI Unanswered Questions."""

	def setUp(self):
		"""Today: one router answer with 👎 and nothing found, one LLM answer."""
		make_conversation(
			[
				{"role": "User", "content": QUESTION},
				{"role": "Assistant", "content": "No records", "answered_by": "Router", "no_result": 1, "feedback": "Down", "response_time": 1.0},
				{"role": "User", "content": "hello"},
				{"role": "Assistant", "content": "Hi", "answered_by": "LLM", "response_time": 3.0},
			]
		)  # fmt: skip
		self.filters = {"from_date": nowdate(), "to_date": nowdate()}

	def test_usage_report(self):
		"""Today's row counts router/LLM answers, feedback and no-result answers."""
		columns, data, _message, chart, summary = usage_report(self.filters)
		self.assertIn("router", [c["fieldname"] for c in columns])
		today = data[-1]
		self.assertGreaterEqual(today.router, 1)
		self.assertGreaterEqual(today.llm, 1)
		self.assertGreaterEqual(today.thumbs_down, 1)
		self.assertGreaterEqual(today.no_result, 1)
		self.assertEqual(chart["type"], "bar")
		self.assertEqual(summary[0]["label"], "Answers")

	def test_usage_report_empty_range(self):
		"""A range with no answers gives no rows and no chart."""
		_columns, data, _message, chart, summary = usage_report(
			{"from_date": "2000-01-01", "to_date": "2000-01-02"}
		)
		self.assertEqual((data, chart), ([], None))
		self.assertEqual(summary[0]["value"], 0)

	def test_unanswered_report_pairs_question_and_answer(self):
		"""Weak answers are listed next to the question that came before them."""
		_columns, rows = unanswered_report(self.filters)
		row = next(r for r in rows if r.question == QUESTION)
		self.assertEqual(row.reason, "👎")
		self.assertEqual(row.answer, "No records")
		self.assertNotIn("hello", [r.question for r in rows])


class TestDailyDigest(IntegrationTestCase):
	"""Morning email of what needs each user's attention."""

	def setUp(self):
		"""A digest role held by the test user, who has one open task."""
		self.role = make_role("SAI Digest Test Role")
		make_test_user(roles=[self.role])
		make_todo("digest test", allocated_to=TEST_EMAIL)

	def tearDown(self):
		"""Back to Administrator with fresh settings."""
		frappe.set_user("Administrator")
		clear_settings_cache()

	def set_digest(self, enabled):
		"""Turns the digest on/off for the test role."""
		settings = frappe.get_single("Support AI Settings")
		settings.enable_daily_digest = enabled
		settings.digest_role = self.role
		settings.save(ignore_permissions=True)

	def test_digest_emails_users_with_pending_work(self):
		"""Users with the role and open tasks get one email with absolute links."""
		self.set_digest(1)
		with patch("frappe.sendmail") as sendmail:
			send_daily_digest()
		calls = [c for c in sendmail.call_args_list if c.kwargs["recipients"] == [TEST_EMAIL]]
		self.assertEqual(len(calls), 1)
		self.assertIn("tasks waiting for you", calls[0].kwargs["subject"])
		self.assertNotIn('href="/app/', calls[0].kwargs["message"])
		self.assertEqual(frappe.session.user, "Administrator")

	def test_digest_off_sends_nothing(self):
		"""With the digest disabled, no email is sent."""
		self.set_digest(0)
		with patch("frappe.sendmail") as sendmail:
			send_daily_digest()
		sendmail.assert_not_called()


class TestSettingsDefaults(IntegrationTestCase):
	"""Defaults for settings added in an update."""

	def tearDown(self):
		"""Fresh settings for the next test."""
		clear_settings_cache()

	def stored(self, fieldname):
		"""Raw value saved for a Support AI Settings field, or None."""
		# tabSingles has no creation column, so get_value's default ordering fails.
		rows = frappe.db.sql(
			"select value from `tabSingles` where doctype = %s and field = %s",
			("Support AI Settings", fieldname),
		)
		return rows[0][0] if rows else None

	def test_missing_values_get_defaults(self):
		"""A never-saved field gets its default on migrate."""
		frappe.db.delete("Singles", {"doctype": "Support AI Settings", "field": "enable_streaming"})
		ensure_settings_defaults()
		self.assertEqual(str(self.stored("enable_streaming")), "1")

	def test_admin_values_are_kept(self):
		"""A value the admin set is never overwritten."""
		frappe.db.set_single_value("Support AI Settings", "rate_limit_per_minute", 7)
		ensure_settings_defaults()
		self.assertEqual(str(self.stored("rate_limit_per_minute")), "7")
