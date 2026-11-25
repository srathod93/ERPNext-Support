# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Regression tests for the Support AI router: real questions and the plan
or answer they must produce. Add a case here whenever a question is
answered wrongly (see the Support AI Unanswered Questions report).

Run: bench --site <site> run-tests --app support_ai --module support_ai.tests.test_router
(needs `allow_tests` in the site config - use a test site, not production)."""

import datetime

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from support_ai.diagnostics.answers import run_planned_lookup
from support_ai.diagnostics.filters import detect_amount_filter, detect_date_range
from support_ai.diagnostics.masking import is_sensitive, mask_value
from support_ai.diagnostics.router import plan_lookup
from support_ai.diagnostics.vocabulary import extract_entities, find_document, tokens_of

TEST_EMAIL = "support-ai-router-test@example.com"
HAS_ERPNEXT = "erpnext" in frappe.get_installed_apps()


class TestEntities(IntegrationTestCase):
	"""Things typed verbatim must survive intact."""

	def test_document_id_is_not_split(self):
		"""ACC-PINV-2026-17381 stays one ID; '2026' must not become a year filter."""
		_emails, ids, _numbers, rest = extract_entities(
			"Purchase Invoice Id : ACC-PINV-2026-17381 find the supplier"
		)
		self.assertEqual(ids, ["ACC-PINV-2026-17381"])
		self.assertNotIn("2026", tokens_of(rest))

	def test_email_is_not_split(self):
		"""An email is one entity, not 'api user jfl erpnext com'."""
		emails, _ids, _numbers, rest = extract_entities(
			"invoice created from setlmint-api-user@jfl-erpnext.com"
		)
		self.assertEqual(emails, ["setlmint-api-user@jfl-erpnext.com"])
		self.assertNotIn("erpnext", tokens_of(rest))

	def test_dates_are_not_ids(self):
		"""15/01/2026 and 2026-01-15 are dates, not document IDs."""
		_emails, ids, _numbers, _rest = extract_entities("invoices between 15/01/2026 and 2026-02-15")
		self.assertEqual(ids, [])


class TestWording(IntegrationTestCase):
	"""Date and amount wording."""

	def test_last_n_days(self):
		"""'last 7 days' ends today and covers 7 days."""
		(start, end), _label, _used = detect_date_range(tokens_of("invoices last 7 days"))
		self.assertEqual(end, getdate(nowdate()))
		self.assertEqual(start, getdate(add_days(nowdate(), -6)))

	def test_between_explicit_dates(self):
		"""'between 1 sep 2026 and 15 sep 2026' is that exact range."""
		text = "invoices between 1 sep 2026 and 15 sep 2026"
		(start, end), _label, _used = detect_date_range(tokens_of(text), text)
		self.assertEqual((start, end), (datetime.date(2026, 9, 1), datetime.date(2026, 9, 15)))

	def test_this_quarter(self):
		"""'this quarter' starts on the first day of the current quarter."""
		(start, _end), _label, _used = detect_date_range(tokens_of("orders this quarter"))
		self.assertEqual(start.day, 1)
		self.assertIn(start.month, (1, 4, 7, 10))

	def test_amount_in_lakh(self):
		"""'above 1 lakh' means grand_total > 100000."""
		if not HAS_ERPNEXT:
			self.skipTest("needs ERPNext")
		meta = frappe.get_meta("Purchase Invoice")
		field, condition, _label, _used = detect_amount_filter(meta, "purchase invoices above 1 lakh")
		self.assertEqual((field, condition), ("grand_total", [">", 100000]))


class TestPlans(IntegrationTestCase):
	"""Questions -> plans, using core DocTypes so it runs on any site."""

	def setUp(self):
		"""A test user who owns a couple of ToDos."""
		if not frappe.db.exists("User", TEST_EMAIL):
			frappe.get_doc(
				{"doctype": "User", "email": TEST_EMAIL, "first_name": "Router", "last_name": "Test"}
			).insert(ignore_permissions=True)
		self.todos = [
			frappe.get_doc({"doctype": "ToDo", "description": f"router test {i}"}).insert(
				ignore_permissions=True
			)
			for i in range(2)
		]
		# insert() always sets owner to the session user; make the test user the creator.
		for todo in self.todos:
			frappe.db.set_value("ToDo", todo.name, "owner", TEST_EMAIL, update_modified=False)

	def test_email_becomes_created_by_filter(self):
		"""'latest 3 todos created by <user email>' filters on owner, limit 3."""
		plan = plan_lookup(f"latest 3 todos created by {TEST_EMAIL}")[0]
		self.assertEqual(plan["doctype"], "ToDo")
		self.assertEqual(plan["filters"].get("owner"), TEST_EMAIL)
		self.assertEqual(plan["limit"], 3)

	def test_follow_up_correction_keeps_doctype(self):
		"""'we don't have <email>' after a ToDo question stays on ToDo."""
		plan = plan_lookup(f"we don't have {TEST_EMAIL} this user", history=["show latest todos"])[0]
		self.assertEqual(plan["doctype"], "ToDo")

	def test_find_document_by_exact_name(self):
		"""A typed ID resolves to its document."""
		todo = self.todos[0]
		self.assertEqual(find_document(todo.name, "ToDo"), ("ToDo", todo.name))

	def test_attention_question(self):
		"""'What needs my attention?' is the attention inbox."""
		self.assertEqual(plan_lookup("What needs my attention?")[0]["kind"], "attention")

	def test_diagnosis_and_writes_go_to_llm(self):
		"""'why ...' and 'create ...' are never answered by the read-only router."""
		self.assertIsNone(plan_lookup("why can't I submit this todo"))
		self.assertIsNone(plan_lookup("create a todo for tomorrow"))
		self.assertIsNone(plan_lookup("hi"))

	def test_list_answer_has_table_and_actions(self):
		"""List answers are a markdown table with Open list / Export actions."""
		result = run_planned_lookup(plan_lookup(f"latest todos created by {TEST_EMAIL}"))
		self.assertIn("| ID |", result["text"])
		self.assertIn("/app/todo/", result["text"])
		self.assertEqual({a["type"] for a in result["actions"]}, {"open_list", "export"})

	def test_admin_synonym_names_a_doctype(self):
		"""A Settings synonym ('chore' = ToDo) routes like the DocType name."""
		settings = frappe.get_single("Support AI Settings")
		settings.append("synonyms", {"phrase": "chore", "document_type": "ToDo"})
		settings.save(ignore_permissions=True)
		plan = plan_lookup("show latest 2 chores")[0]
		self.assertEqual((plan["doctype"], plan["limit"]), ("ToDo", 2))

	def test_ambiguous_word_asks_which(self):
		"""'latest invoices' asks Sales or Purchase Invoice."""
		if not HAS_ERPNEXT:
			self.skipTest("needs ERPNext")
		plan = plan_lookup("latest invoices")[0]
		self.assertEqual(plan["kind"], "clarify")
		self.assertEqual({o["doctype"] for o in plan["options"]}, {"Sales Invoice", "Purchase Invoice"})


class TestMasking(IntegrationTestCase):
	"""Sensitive field matching."""

	def test_patterns_match_whole_words(self):
		"""'pan' matches pan / supplier_pan but not company."""
		patterns = ["pan", "bank_account_no"]
		self.assertTrue(is_sensitive("pan", patterns))
		self.assertTrue(is_sensitive("supplier_pan", patterns))
		self.assertFalse(is_sensitive("company", patterns))
		self.assertTrue(is_sensitive("bank_account_no", patterns))

	def test_mask_keeps_last_two_characters(self):
		"""Masked values keep the last 2 characters."""
		self.assertTrue(mask_value("ABCDE1234F").endswith("4F"))
		self.assertNotIn("ABCDE", mask_value("ABCDE1234F"))
