# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Tests for how router plans become answers: document details, counts,
clarifying questions, the attention inbox, child tables, approval history,
markdown formatting and sensitive-field masking."""

import frappe
from frappe.tests import IntegrationTestCase

from support_ai.diagnostics import insights
from support_ai.diagnostics.answers import database_facts, run_planned_lookup
from support_ai.diagnostics.formatting import doc_link, escape_cell, markdown_table, source_line
from support_ai.diagnostics.masking import MASK, is_sensitive, mask_record, mask_value
from support_ai.diagnostics.vocabulary import tokens_of
from support_ai.tests.helpers import (
	HAS_ERPNEXT,
	TEST_EMAIL,
	clear_settings_cache,
	make_role,
	make_test_user,
	make_todo,
)


def answer(plan):
	"""Runs one plan and returns the merged result dict."""
	return run_planned_lookup([plan])


class TestFormatting(IntegrationTestCase):
	"""Markdown building blocks."""

	def test_doc_link(self):
		"""Links point at the Desk form, with the name URL-quoted."""
		self.assertEqual(doc_link("Purchase Invoice", "A B"), "[A B](/app/purchase-invoice/A%20B)")
		self.assertEqual(doc_link(None, "X"), "X")

	def test_table_escapes_pipes_but_keeps_links(self):
		"""Cell pipes/newlines are made safe; links pass through untouched."""
		table = markdown_table(["ID", "Note"], [[doc_link("ToDo", "T1"), "a|b\nc"]])
		self.assertIn("[T1](/app/todo/T1)", table)
		self.assertIn("a/b c", table)
		self.assertEqual(table.splitlines()[1], "|---|---|")
		self.assertEqual(escape_cell(None), "")

	def test_source_line(self):
		"""Italic 'Source:' footer, with optional detail."""
		self.assertEqual(source_line("ToDo"), "\n_Source: ToDo_")
		self.assertEqual(source_line("ToDo", "open"), "\n_Source: ToDo · open_")


class TestMasking(IntegrationTestCase):
	"""Sensitive values are hidden unless the user has the unmask role."""

	def setUp(self):
		"""Settings that mark 'secret_code' sensitive, unmasked for one role."""
		self.unmask_role = make_role("SAI Unmask Test Role")
		settings = frappe.get_single("Support AI Settings")
		settings.sensitive_fieldnames = "secret_code\npan"
		settings.sensitive_unmask_role = self.unmask_role
		settings.save(ignore_permissions=True)
		make_test_user()

	def tearDown(self):
		"""Back to Administrator with fresh settings."""
		frappe.set_user("Administrator")
		clear_settings_cache()

	def test_patterns_match_whole_words(self):
		"""'pan' matches pan / supplier_pan but not company."""
		patterns = ["pan", "bank_account_no"]
		self.assertTrue(is_sensitive("pan", patterns))
		self.assertTrue(is_sensitive("supplier_pan", patterns))
		self.assertFalse(is_sensitive("company", patterns))
		self.assertFalse(is_sensitive("", patterns))

	def test_mask_value(self):
		"""Masked values keep only the last 2 characters; short ones are fully masked."""
		self.assertEqual(mask_value("ABCDE1234F"), MASK + "4F")
		self.assertEqual(mask_value("123"), MASK)
		self.assertIsNone(mask_value(None))

	def test_mask_record_for_user_without_role(self):
		"""Nested dicts/lists are masked; other fields and dict types are kept."""
		frappe.set_user(TEST_EMAIL)
		record = frappe._dict(name="X", secret_code="ABCDE1234F", rows=[{"pan": "AAAPL1234C", "qty": 1}])
		masked = mask_record(record)
		self.assertIsInstance(masked, frappe._dict)
		self.assertEqual(masked.name, "X")
		self.assertEqual(masked.secret_code, MASK + "4F")
		self.assertEqual(masked.rows[0], {"pan": MASK + "4C", "qty": 1})

	def test_unmask_role_sees_real_values(self):
		"""A user with the unmask role sees the real value."""
		make_test_user(roles=[self.unmask_role])
		frappe.set_user(TEST_EMAIL)
		self.assertEqual(mask_record({"secret_code": "ABCDE1234F"})["secret_code"], "ABCDE1234F")


class TestDocumentAnswers(IntegrationTestCase):
	"""One-document answers and other non-list answers."""

	def setUp(self):
		"""A ToDo and a Role with a hyphenated, digit-bearing name."""
		make_test_user()
		self.todo = make_todo("answer test", owner=TEST_EMAIL)
		self.role = make_role("SAI-TEST-0001")

	def test_document_details(self):
		"""Details list key fields, who created it and follow-up chips."""
		result = answer({"kind": "document", "doctype": "ToDo", "name": self.todo.name, "tokens": []})
		self.assertIn(f"/app/todo/{self.todo.name}", result["text"])
		self.assertIn("**Created By:** Router Test", result["text"])
		self.assertEqual({a["type"] for a in result["actions"]}, {"suggestion"})

	def test_child_table_rows(self):
		"""'roles of user X' shows that user's Has Role rows."""
		make_test_user(roles=[self.role])
		result = answer(
			{"kind": "document", "doctype": "User", "name": TEST_EMAIL, "tokens": tokens_of("roles")}
		)
		self.assertIn("rows)**", result["text"])
		self.assertIn(self.role, result["text"])

	def test_workflow_history_without_workflow(self):
		"""A document with no workflow changes says so."""
		result = answer(
			{"kind": "document", "doctype": "ToDo", "name": self.todo.name, "tokens": ["history"]}
		)
		self.assertIn("Approval history", result["text"])
		self.assertIn("No workflow or status changes", result["text"])

	def test_missing_id_and_user_info(self):
		"""Unknown IDs and emails get a plain explanation."""
		missing = answer({"kind": "missing_id", "doc_id": "ZZQ-1", "doctype": "ToDo"})
		self.assertTrue(missing["no_result"])
		self.assertIn("No record with ID 'ZZQ-1' was found in ToDo", missing["text"])
		self.assertIn("is not a User", answer({"kind": "user_info", "email": "zzq@example.com"})["text"])
		self.assertIn("Router Test", answer({"kind": "user_info", "email": TEST_EMAIL})["text"])

	def test_clarify(self):
		"""Clarifying answers offer one suggestion chip per option."""
		plan = {
			"kind": "clarify",
			"word": "invoice",
			"options": [
				{"doctype": "Sales Invoice", "prompt": "a"},
				{"doctype": "Purchase Invoice", "prompt": "b"},
			],
		}
		result = answer(plan)
		self.assertIn("Sales Invoice or Purchase Invoice", result["text"])
		self.assertEqual([a["prompt"] for a in result["actions"]], ["a", "b"])

	def test_database_facts_for_typed_ids_and_emails(self):
		"""The LLM gets real data for IDs/emails typed in the question."""
		facts = database_facts(f"why can't role {self.role} see this, asked by {TEST_EMAIL}")
		self.assertIn(f"Role {doc_link('Role', self.role)}", facts)
		self.assertIn(f"User {TEST_EMAIL}", facts)
		self.assertIn("No record with ID 'ZZQ-NOPE-0001'", database_facts("what is ZZQ-NOPE-0001"))
		self.assertIsNone(database_facts("why is it slow"))

	def test_empty_list_is_no_result(self):
		"""A list with no rows says so and is flagged as no result."""
		plan = {
			"kind": "list", "doctype": "ToDo", "filters": {"description": "zzq-nothing"}, "child_filters": [],
			"limit": 5, "order_by": "creation desc", "is_count": False, "is_sum": False, "sum_field": None,
			"error": None, "requested_fields": [], "label": "x", "date_field": None, "value_field": None,
			"name_words": [], "name_field": None, "show_owner": False,
		}  # fmt: skip
		result = answer(plan)
		self.assertTrue(result["no_result"])
		self.assertIn("No ToDo records found", result["text"])


class TestInsights(IntegrationTestCase):
	"""Ready-made answers."""

	def setUp(self):
		"""A user with one open ToDo assigned to them."""
		make_test_user()
		make_todo("attention test", allocated_to=TEST_EMAIL)

	def test_attention_summary_counts(self):
		"""Open ToDos assigned to the user are counted and listed."""
		summary = insights.attention_summary(TEST_EMAIL)
		self.assertGreaterEqual(summary["counts"]["todos"], 1)
		self.assertIn("Open tasks", summary["text"])

	def test_intent_helpers(self):
		"""Word helpers for attention and sum questions."""
		self.assertTrue(insights.wants_attention(tokens_of("what needs my attention")))
		self.assertFalse(insights.wants_attention(tokens_of("latest todos")))
		self.assertTrue(insights.wants_sum(tokens_of("total amount of invoices")))
		self.assertFalse(insights.wants_sum(tokens_of("total number of invoices")))

	def test_sum_field(self):
		"""'total outstanding' sums outstanding_amount; default grand_total."""
		if not HAS_ERPNEXT:
			self.skipTest("needs ERPNext")
		meta = frappe.get_meta("Purchase Invoice")
		self.assertEqual(insights.sum_field(meta, ["outstanding"]), "outstanding_amount")
		self.assertEqual(insights.sum_field(meta, ["amount"]), "grand_total")
		self.assertIsNone(insights.sum_field(frappe.get_meta("ToDo"), ["amount"]))

	def test_outstanding_and_stock(self):
		"""Payable summary and stock balance answer without errors."""
		if not HAS_ERPNEXT:
			self.skipTest("needs ERPNext")
		payable = insights.outstanding_summary("Supplier")
		self.assertTrue("Accounts Payable" in payable or "No outstanding" in payable)
		self.assertIn("No stock found", insights.stock_balance(["zzq-no-such-item"]))
