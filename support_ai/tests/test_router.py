# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Regression tests for the Support AI router: real questions and the plan
they must produce. Add a case here whenever a question is answered wrongly
(see the Support AI Unanswered Questions report).

Run: bench --site <site> run-tests --app support_ai --module support_ai.tests.test_router
(needs `allow_tests` in the site config - use a test site, not production)."""

import frappe
from frappe.tests import IntegrationTestCase

from support_ai.diagnostics.answers import run_planned_lookup
from support_ai.diagnostics.router import plan_lookup
from support_ai.diagnostics.vocabulary import (
	extract_entities,
	find_document,
	has_fuzzy_intent,
	previous_doctype,
	tokens_of,
)
from support_ai.tests.helpers import HAS_ERPNEXT, TEST_EMAIL, clear_settings_cache, make_test_user, make_todo


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

	def test_long_numbers_are_kept_separately(self):
		"""A bare 9-digit code (e.g. a Supplier ID) is offered as a number, not an ID."""
		_emails, ids, numbers, _rest = extract_entities("supplier 810000011 details")
		self.assertEqual((ids, numbers), ([], ["810000011"]))


class TestPlans(IntegrationTestCase):
	"""Questions -> plans, using core DocTypes so it runs on any site."""

	def setUp(self):
		"""A test user who created a couple of ToDos."""
		make_test_user()
		self.todos = [make_todo(f"router test {i}", owner=TEST_EMAIL) for i in range(2)]

	def tearDown(self):
		"""Settings may have been changed by a test."""
		clear_settings_cache()

	def test_email_becomes_created_by_filter(self):
		"""'latest 3 todos created by <user email>' filters on owner, limit 3."""
		plan = plan_lookup(f"latest 3 todos created by {TEST_EMAIL}")[0]
		self.assertEqual(plan["doctype"], "ToDo")
		self.assertEqual(plan["filters"].get("owner"), TEST_EMAIL)
		self.assertEqual(plan["limit"], 3)
		self.assertTrue(plan["show_owner"])

	def test_unknown_email_without_doctype_is_user_info(self):
		"""An email alone with no DocType asks about the user."""
		plan = plan_lookup("nobody-here@example.com")[0]
		self.assertEqual(plan, {"kind": "user_info", "email": "nobody-here@example.com"})

	def test_follow_up_correction_keeps_doctype(self):
		"""'we don't have <email>' after a ToDo question stays on ToDo."""
		plan = plan_lookup(f"we don't have {TEST_EMAIL} this user", history=["show latest todos"])[0]
		self.assertEqual(plan["doctype"], "ToDo")

	def test_previous_doctype_skips_messages_without_one(self):
		"""The most recent earlier message that names a DocType wins."""
		self.assertEqual(previous_doctype(["show latest todos", "thanks"])[0], "ToDo")
		self.assertIsNone(previous_doctype(["hello", "thanks"]))

	def test_find_document_by_exact_name(self):
		"""A typed ID resolves to its document; an unknown one to None."""
		todo = self.todos[0]
		self.assertEqual(find_document(todo.name, "ToDo"), ("ToDo", todo.name))
		self.assertIsNone(find_document("NO-SUCH-0001", "ToDo"))

	def test_unknown_id_gives_missing_plan(self):
		"""An ID that matches nothing says so instead of guessing."""
		plan = plan_lookup("todo ZZQ-NOPE-2026-0001")[0]
		self.assertEqual(plan["kind"], "missing_id")
		self.assertEqual(plan["doc_id"], "ZZQ-NOPE-2026-0001")

	def test_attention_question(self):
		"""'What needs my attention?' and 'my tasks' are the attention inbox."""
		self.assertEqual(plan_lookup("What needs my attention?")[0]["kind"], "attention")
		self.assertEqual(plan_lookup("show my tasks")[0]["kind"], "attention")
		self.assertEqual(plan_lookup("my todo list")[0]["kind"], "attention")
		# A bare "todo" is the ToDo DocType, not the inbox.
		self.assertEqual(plan_lookup("find todo router test")[0]["kind"], "list")

	def test_diagnosis_and_writes_go_to_llm(self):
		"""'why ...', 'how to ...' and 'create ...' are never answered by the read-only router."""
		self.assertIsNone(plan_lookup("why can't I submit this todo"))
		self.assertIsNone(plan_lookup("how to close a todo"))
		self.assertIsNone(plan_lookup("create a todo for tomorrow"))
		self.assertIsNone(plan_lookup("delete my todos"))
		self.assertIsNone(plan_lookup("hi"))

	def test_doctype_alone_is_a_data_question(self):
		"""Naming a DocType with filters but no intent word is still a lookup."""
		plan = plan_lookup("todos this month")[0]
		self.assertEqual(plan["doctype"], "ToDo")
		self.assertIn("date", " ".join(plan["filters"]) + (plan["date_field"] or ""))

	def test_status_value_becomes_filter(self):
		"""'closed todos' filters status = Closed."""
		plan = plan_lookup("show closed todos")[0]
		self.assertEqual(plan["filters"].get("status"), "Closed")

	def test_unknown_status_lists_valid_values(self):
		"""A status the DocType doesn't have is an error listing the real ones."""
		plan = plan_lookup("todos with status frozen")[0]
		self.assertIn("not a status of ToDo", plan["error"])
		self.assertIn("Open", plan["error"])

	def test_leftover_words_become_name_search(self):
		"""Words that are not filters search the title/name field."""
		plan = plan_lookup("find todo router test")[0]
		self.assertEqual(plan["name_words"], ["router", "test"])

	def test_limits_and_order(self):
		"""'any' -> 1, 'all' -> 10, default 5; 'oldest' sorts ascending."""
		self.assertEqual(plan_lookup("give any todo")[0]["limit"], 1)
		self.assertEqual(plan_lookup("list all todos")[0]["limit"], 10)
		self.assertEqual(plan_lookup("show todos")[0]["limit"], 5)
		self.assertEqual(plan_lookup("oldest todos")[0]["order_by"], "creation asc")

	def test_count_question(self):
		"""'how many todos' is a count, answered with the number."""
		plan = plan_lookup("how many todos")[0]
		self.assertTrue(plan["is_count"])
		self.assertIn("ToDo:", run_planned_lookup([plan])["text"])

	def test_typo_of_intent_word(self):
		"""'lasted todo' (typo of latest) is still a lookup."""
		self.assertEqual(plan_lookup("lasted todo")[0]["doctype"], "ToDo")
		# Short words that merely look like "latest" are not typos of it.
		self.assertFalse(has_fuzzy_intent(["test"]))
		self.assertTrue(has_fuzzy_intent(["lates"]))

	def test_list_view_context(self):
		"""On a list view, a question without a DocType uses that list."""
		context = {"doctype": "ToDo", "filters": [["ToDo", "status", "=", "Open"]]}
		plan = plan_lookup("how many of these", context=context)[0]
		self.assertEqual(plan["doctype"], "ToDo")
		self.assertEqual(plan["filters"].get("status"), ["=", "Open"])

	def test_form_reference_document_questions(self):
		"""On a form, 'who created this' is about that document; 'why' goes to the LLM."""
		todo = self.todos[0]
		plan = plan_lookup("who created this", reference=("ToDo", todo.name))[0]
		self.assertEqual((plan["kind"], plan["name"]), ("document", todo.name))
		self.assertIsNone(plan_lookup("it is not saving", reference=("ToDo", todo.name)))

	def test_list_answer_has_table_and_actions(self):
		"""List answers are a markdown table with Open list / Export actions."""
		result = run_planned_lookup(plan_lookup(f"latest todos created by {TEST_EMAIL}"))
		self.assertIn("| ID |", result["text"])
		self.assertIn("/app/todo/", result["text"])
		self.assertIn("_Source:", result["text"])
		self.assertEqual({a["type"] for a in result["actions"]}, {"open_list", "export"})
		self.assertFalse(result["no_result"])

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
		self.assertEqual(plan["options"][0]["prompt"].lower().count("invoice"), 1)

	def test_built_in_aliases(self):
		"""'grn' means Purchase Receipt, 'po' Purchase Order."""
		if not HAS_ERPNEXT:
			self.skipTest("needs ERPNext")
		self.assertEqual(plan_lookup("latest 3 grn")[0]["doctype"], "Purchase Receipt")
		self.assertEqual(plan_lookup("show po")[0]["doctype"], "Purchase Order")

	def test_outstanding_and_stock_plans(self):
		"""Ready-made plans for payable/receivable and stock questions."""
		if not HAS_ERPNEXT:
			self.skipTest("needs ERPNext")
		self.assertEqual(plan_lookup("total outstanding of suppliers")[0]["party_type"], "Supplier")
		self.assertEqual(plan_lookup("receivable of customers")[0]["party_type"], "Customer")
		self.assertEqual(plan_lookup("stock of item zzq-no-such-item")[0]["kind"], "stock")
