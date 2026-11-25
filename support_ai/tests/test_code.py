# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Tests for reading this site's customization code: the error -> source
search, the per-DocType customization map, code access control, the
automatic code grounding for "why / error" questions and the
"search code for ..." command. Test scripts and source files are created
inside the test (rolled back / temporary), so no real custom app is needed."""

import os
import tempfile
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from support_ai.ai.tools import get_tools
from support_ai.api import ask, new_conversation
from support_ai.diagnostics import code_search
from support_ai.diagnostics.answers import run_planned_lookup
from support_ai.diagnostics.code_context import code_grounding, error_text
from support_ai.diagnostics.code_search import can_read_code, redact, search_custom_code, search_fragments
from support_ai.diagnostics.customization_map import _MapBuilder, build_customization_map
from support_ai.diagnostics.router import code_search_query, plan_lookup
from support_ai.tests.helpers import TEST_EMAIL, clear_settings_cache, make_role, make_test_user

SCRIPT_ERROR = "Zzq widget colour must be blue"
TEMPLATE_SCRIPT = 'label = "Shade"\nfrappe.throw(f"{label} zzq cannot be purple today")\n'
FILE_SOURCE = 'import frappe\n\n\ndef check_zzq(doc):\n\t"""Test fixture."""\n\traise frappe.ValidationError("zzq file level error text")\n'


def make_server_script(name, script, script_type="API", **extra):
	"""Creates an enabled Server Script that never runs during the tests
	(an API method nobody calls, or an event ToDo never fires)."""
	if frappe.db.exists("Server Script", name):
		# Data rolls back per test class, so a sibling test may have made it.
		frappe.db.set_value("Server Script", name, {"script": script, "disabled": 0})
		return frappe.get_doc("Server Script", name)
	values = {"doctype": "Server Script", "name": name, "script_type": script_type, "script": script, **extra}
	if script_type == "API":
		values["api_method"] = frappe.scrub(name)
	return frappe.get_doc(values).insert(ignore_permissions=True)


class CodeTestCase(IntegrationTestCase):
	"""Common setup: code access for Administrator, a test user without it."""

	def setUp(self):
		"""Default code access role and a plain test user."""
		settings = frappe.get_single("Support AI Settings")
		settings.code_access_role = "System Manager"
		settings.code_context_chars = 6000
		settings.rate_limit_per_minute = 0
		settings.enable_streaming = 0
		settings.save(ignore_permissions=True)
		make_test_user()
		code_search._file_cache["at"] = None

	def tearDown(self):
		"""Back to Administrator with fresh settings and file list."""
		frappe.set_user("Administrator")
		clear_settings_cache()
		code_search._file_cache["at"] = None


class TestSearchHelpers(CodeTestCase):
	"""Pure helpers of the code search."""

	def test_fragments_include_whole_text_and_pieces(self):
		"""The whole message comes first, then the parts a template fills in."""
		fragments = search_fragments("Row 3: Amount 500 is more than allowed limit")
		self.assertEqual(fragments[0], "row 3: amount 500 is more than allowed limit")
		self.assertIn("is more than allowed limit", fragments)
		self.assertIn("more than allowed", fragments)

	def test_fragments_need_some_text(self):
		"""Too-short queries give nothing to search."""
		self.assertEqual(search_fragments("ab"), [])
		self.assertEqual(search_fragments(""), [])

	def test_redact_hides_credentials(self):
		"""Hard-coded keys/passwords are hidden; ordinary code is untouched."""
		self.assertEqual(redact('api_key = "sk-abcdef123456"'), 'api_key = "***"')
		self.assertEqual(redact("client_secret: 'zzz999zzz'"), "client_secret: '***'")
		self.assertEqual(redact('label = "Shade"'), 'label = "Shade"')

	def test_error_text_extraction(self):
		"""The error part of a question: after 'Error:', else quoted, else all."""
		self.assertEqual(error_text("Why? Error: Last Name cannot be empty"), "Last Name cannot be empty")
		self.assertEqual(
			error_text('I get "Amount must be positive here" on save'), "Amount must be positive here"
		)
		self.assertEqual(error_text("stock is negative"), "stock is negative")


class TestCodeSearch(CodeTestCase):
	"""search_custom_code over scripts and source files."""

	def setUp(self):
		"""Two API Server Scripts that raise known messages."""
		super().setUp()
		make_server_script("SAI Zzq Plain", f'frappe.throw("{SCRIPT_ERROR}")\n')
		make_server_script("SAI Zzq Template", TEMPLATE_SCRIPT)

	def test_exact_error_in_server_script(self):
		"""The full error text finds the Server Script line that raises it."""
		result = search_custom_code(f"{SCRIPT_ERROR}.")
		match = result["matches"][0]
		self.assertEqual(
			(match["source"], match["path"], match["line"]), ("Server Script", "SAI Zzq Plain", 1)
		)
		self.assertIn("frappe.throw", match["snippet"])

	def test_filled_in_template_still_matches(self):
		"""'Shade zzq cannot be purple today' finds f'{label} zzq cannot be purple today'."""
		result = search_custom_code("Why do I get: Colour zzq cannot be purple today")
		self.assertEqual(result["matches"][0]["path"], "SAI Zzq Template")

	def test_disabled_scripts_are_skipped(self):
		"""Disabled Server Scripts are not searched."""
		frappe.db.set_value("Server Script", "SAI Zzq Plain", "disabled", 1)
		paths = [m["path"] for m in search_custom_code(SCRIPT_ERROR)["matches"]]
		self.assertNotIn("SAI Zzq Plain", paths)

	def test_source_files_with_function_name(self):
		"""Matches in custom app files give the relative path and enclosing function."""
		with tempfile.TemporaryDirectory() as root:
			path = os.path.join(root, "fake_app", "checks.py")
			os.makedirs(os.path.dirname(path))
			with open(path, "w") as f:
				f.write(FILE_SOURCE)
			files = [("fake_app", root, path)]
			with patch.object(code_search, "_walk_custom_apps", return_value=iter(files)):
				result = search_custom_code("zzq file level error text")
		match = result["matches"][0]
		self.assertEqual(
			(match["source"], match["path"], match["function"]),
			("fake_app", "fake_app/checks.py", "check_zzq"),
		)
		self.assertEqual(match["line"], 6)

	def test_limit(self):
		"""limit caps the number of matches."""
		self.assertEqual(len(search_custom_code("zzq widget colour must be", limit=1)["matches"]), 1)

	def test_no_access_without_role(self):
		"""Users without the code access role cannot search."""
		frappe.set_user(TEST_EMAIL)
		self.assertFalse(can_read_code())
		self.assertIn("permission", search_custom_code(SCRIPT_ERROR)["error"])

	def test_custom_code_role(self):
		"""Settings > Code Access Role decides who may read code."""
		role = make_role("SAI Code Reader Test")
		settings = frappe.get_single("Support AI Settings")
		settings.code_access_role = role
		settings.save(ignore_permissions=True)
		make_test_user(roles=[role])
		frappe.set_user(TEST_EMAIL)
		self.assertTrue(can_read_code())


class TestCustomizationMap(CodeTestCase):
	"""build_customization_map for one DocType."""

	FAKE_HOOKS = {
		"doc_events": {
			"ToDo": {
				"validate": "support_ai.diagnostics.formatting.escape_cell",
				"on_submit": ["support_ai.diagnostics.formatting.source_line"],
			},
			"*": {"on_update": "support_ai.diagnostics.formatting.markdown_table"},
		},
		"override_doctype_class": {"ToDo": ["support_ai.tests.helpers.make_todo"]},
		"doctype_js": {"ToDo": "public/js/todo.js"},
		"has_permission": {"ToDo": "support_ai.diagnostics.formatting.doc_link"},
		"override_whitelisted_methods": {
			"frappe.desk.doctype.todo.todo.new_todo": "support_ai.tests.helpers.make_todo",
			"frappe.core.doctype.user.user.update_password": "support_ai.tests.helpers.make_test_user",
		},
	}

	def setUp(self):
		"""A Server Script on ToDo for an event ToDo never fires."""
		super().setUp()
		make_server_script(
			"SAI Zzq ToDo Hook", 'frappe.throw("zzq todo hook")\n', script_type="DocType Event",
			reference_doctype="ToDo", doctype_event="Before Submit",
		)  # fmt: skip

	def build(self, **kwargs):
		"""Map of ToDo as if one custom app 'fake_app' had FAKE_HOOKS."""
		hooks = lambda app, hook: self.FAKE_HOOKS.get(hook)
		with (
			patch(
				"support_ai.diagnostics.customization_map.get_installed_custom_apps",
				return_value=["fake_app"],
			),
			patch("support_ai.diagnostics.customization_map._app_hook", side_effect=hooks),
		):
			return build_customization_map("ToDo", **kwargs)

	def test_hooks_include_real_source(self):
		"""Every hook kind is listed, with the handler's source code."""
		result = self.build(budget=20000)
		handlers = {h["handler"]: h for h in result["doc_event_hooks"]}
		self.assertIn("def escape_cell", handlers["support_ai.diagnostics.formatting.escape_cell"]["source"])
		self.assertEqual(handlers["support_ai.diagnostics.formatting.markdown_table"]["applies_to"], "*")
		self.assertEqual({o["hook"] for o in result["overrides"]}, {"override_doctype_class", "doctype_js"})
		self.assertNotIn("source", next(o for o in result["overrides"] if o["hook"] == "doctype_js"))
		self.assertEqual(result["permission_hooks"][0]["hook"], "has_permission")
		self.assertIn("zzq todo hook", result["server_scripts"][0]["script"])

	def test_only_this_doctypes_method_overrides(self):
		"""A whitelisted-method override is listed only for its own DocType."""
		originals = [o["original"] for o in self.build(budget=20000)["whitelisted_method_overrides"]]
		self.assertEqual(originals, ["frappe.desk.doctype.todo.todo.new_todo"])

	def test_focus_puts_asked_event_first(self):
		"""'why can't I submit' puts on_submit hooks first."""
		events = [h["event"] for h in self.build(budget=20000, focus="why can't I submit")["doc_event_hooks"]]
		self.assertEqual(events[0], "on_submit")

	def test_budget_leaves_code_out_and_says_so(self):
		"""A tiny budget keeps the list but drops code, with a note."""
		result = self.build(budget=50)
		self.assertTrue(all("source" not in h for h in result["doc_event_hooks"]))
		self.assertIn("Left out to fit", result["_notes"][0])

	def test_without_code_role_only_names(self):
		"""Users without the code role see hook names but no code."""
		frappe.set_user(TEST_EMAIL)
		result = self.build(budget=20000)
		self.assertTrue(all("source" not in h for h in result["doc_event_hooks"]))
		self.assertTrue(all("script" not in s for s in result["server_scripts"]))
		self.assertIn("code access role", result["_notes"][0])

	def test_unknown_doctype(self):
		"""A name that is not a DocType gives an error."""
		self.assertIn("error", build_customization_map("No Such DocType Zzq"))

	def test_fit_trims_configuration_before_code(self):
		"""Oversized maps lose custom fields before hook entries."""
		builder = _MapBuilder("ToDo", 100, True)
		out = {
			"custom_fields": [f"field_{i} (Data)" for i in range(200)],
			"doc_event_hooks": [{"event": "validate"}] * 3,
		}
		builder._fit(out)
		self.assertLess(len(out["custom_fields"]), 200)
		self.assertEqual(len(out["doc_event_hooks"]), 3)


class TestCodeInChat(CodeTestCase):
	"""Code search command, AI tools and automatic grounding."""

	def setUp(self):
		"""A Server Script with a known error."""
		super().setUp()
		make_server_script("SAI Zzq Plain", f'frappe.throw("{SCRIPT_ERROR}")\n')

	def test_search_command_patterns(self):
		"""Several ways of asking to search code; other text is not a command."""
		self.assertEqual(code_search_query("search code for Zzq widget"), "Zzq widget")
		self.assertEqual(code_search_query('find in code: "abc def"'), "abc def")
		self.assertEqual(code_search_query("code search - abc"), "abc")
		self.assertEqual(code_search_query("where is this error coming from: abc def"), "abc def")
		self.assertEqual(code_search_query("search code"), "")
		self.assertIsNone(code_search_query("latest purchase invoices"))

	def test_search_command_answer(self):
		"""The router answers 'search code for ...' with file:line and a code block."""
		result = run_planned_lookup(plan_lookup(f"search code for {SCRIPT_ERROR}"))
		self.assertIn("in custom code", result["text"])
		self.assertIn("`SAI Zzq Plain:1`", result["text"])
		self.assertIn("```", result["text"])
		empty = run_planned_lookup(plan_lookup("search code"))
		self.assertTrue(empty["no_result"])

	def test_code_tools_only_for_code_role(self):
		"""The AI gets search_custom_code / get_customization_map only with the code role."""
		names = {t.name for t in get_tools(None, False, None)}
		self.assertTrue({"search_custom_code", "get_customization_map"} <= names)
		frappe.set_user(TEST_EMAIL)
		names = {t.name for t in get_tools(None, False, None)}
		self.assertFalse({"search_custom_code", "get_customization_map"} & names)

	def test_grounding_text(self):
		"""Grounding has the code location and, for a named DocType, its map."""
		text = code_grounding(f"Why do I get this on ToDo? Error: {SCRIPT_ERROR}")
		self.assertIn("[Server Script] SAI Zzq Plain:1", text)
		self.assertIn("Customizations of ToDo", text)
		frappe.set_user(TEST_EMAIL)
		self.assertIsNone(code_grounding(f"Error: {SCRIPT_ERROR}"))

	def test_why_question_sends_code_to_model(self):
		"""A 'why' question puts the code context into the model's messages; a data question does not."""
		seen = []

		def fake_engine(config, messages, tools, ctx, max_rounds=2):
			"""Captures what the model would receive."""
			seen.append("\n".join(m["content"] for m in messages if m["role"] == "system"))
			return {"content": "ok", "thinking": None}

		convo = new_conversation(title="code test")
		with patch("support_ai.ai.runner.run_builtin", side_effect=fake_engine):
			ask(convo, f"Why am I getting this error? Error: {SCRIPT_ERROR}")
			ask(convo, "hello, what can you do")
		self.assertIn("Code context from this site", seen[0])
		self.assertIn(
			"MOST LIKELY SOURCE: this message is raised by the Server Script 'SAI Zzq Plain'", seen[0]
		)
		self.assertNotIn("Code context from this site", seen[1])

	def test_exact_match_puts_source_line_on_reply(self):
		"""An exact error match adds a linked source line the model can't get wrong."""
		convo = new_conversation(title="code test")
		with patch(
			"support_ai.ai.runner.run_builtin", return_value={"content": "Because.", "thinking": None}
		):
			reply = ask(convo, f"Why? Error: {SCRIPT_ERROR}")["reply"]
		self.assertTrue(
			reply.startswith(
				"**Source of this error:** Server Script [SAI Zzq Plain](/app/server-script/SAI%20Zzq%20Plain), line 1."
			)
		)
		self.assertIn("Because.", reply)
		with patch("support_ai.ai.runner.run_builtin", return_value={"content": "Hmm.", "thinking": None}):
			reply = ask(convo, "Why? Error: zzq colour widget blue must")["reply"]
		self.assertFalse(reply.startswith("**Source of this error:**"))
