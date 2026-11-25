# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Ready-made answers the router can give from real data, beyond plain
lists: "what needs my attention", a document's workflow/approval history,
its child-table rows, stock balance of an item, supplier/customer
outstanding (Accounts Payable / Receivable style) and sums. All reads go
through permission-checked APIs (get_list / has_permission)."""

import json

import frappe
from frappe.utils import flt, fmt_money, format_datetime

from support_ai.diagnostics.formatting import doc_link, markdown_table, source_line
from support_ai.diagnostics.masking import mask_record

ATTENTION_WORDS = {
	"attention",
	"inbox",
	"pending approvals",
	"my approvals",
	"my tasks",
	"my todos",
	"my todo",
	"my to do",
	# A bare "todo" names the ToDo DocType ("find todo X"), so only "my ..." counts.
}
HISTORY_WORDS = {
	"approved",
	"approval",
	"approvals",
	"approver",
	"history",
	"timeline",
	"rejected",
	"workflow",
}
OUTSTANDING_WORDS = {"outstanding", "payable", "receivable", "dues", "owe", "owed"}
STOCK_WORDS = {"stock", "inventory", "qty", "quantity"}
SUM_PHRASES = ("sum of", "total amount", "total value", "total outstanding", "total paid", "sum")
MAX_ROWS = 20


def wants_attention(tokens):
	"""True for "what needs my attention" / "my pending approvals" style questions."""
	joined = " " + " ".join(tokens) + " "
	return any(f" {w} " in joined for w in ATTENTION_WORDS)


def attention_summary(user=None):
	"""Open workflow actions, open ToDos and unread notifications for a user.

	Parameters:
		user (str, optional): User ID, defaults to the session user.

	Returns:
		dict: {"text": markdown, "actions": [], "counts": {...}}
	"""
	user = user or frappe.session.user
	approvals = _open_workflow_actions(user)
	todos = frappe.get_list(
		"ToDo",
		filters={"allocated_to": user, "status": "Open"},
		fields=["name", "reference_type", "reference_name", "description", "date", "priority"],
		order_by="date asc, creation asc",
		limit_page_length=MAX_ROWS,
	)
	unread = frappe.db.count("Notification Log", {"for_user": user, "read": 0})

	lines = ["**What needs your attention**", ""]
	lines += _approvals_block(approvals) + _todos_block(todos)
	lines.append(f"**Unread notifications:** {unread}")
	if not approvals and not todos and not unread:
		lines = ["Nothing needs your attention right now. 🎉"]
	lines.append(source_line("Workflow Action, ToDo, Notification Log", "assigned to you"))
	counts = {"approvals": len(approvals), "todos": len(todos), "unread": unread}
	return {"text": "\n".join(lines), "actions": [], "counts": counts}


def _open_workflow_actions(user):
	"""Open Workflow Actions the user can act on (the DocType's own permission
	query limits rows to the user's roles/assignments)."""
	if not frappe.db.table_exists("Workflow Action"):
		return []
	return frappe.get_list(
		"Workflow Action",
		filters={"status": "Open"},
		fields=["reference_doctype", "reference_name", "workflow_state", "creation"],
		order_by="creation asc",
		limit_page_length=MAX_ROWS,
	)


def _approvals_block(approvals):
	"""Markdown lines for pending approvals."""
	if not approvals:
		return ["**Pending approvals:** none", ""]
	rows = [
		[
			doc_link(a.reference_doctype, a.reference_name),
			a.reference_doctype,
			a.workflow_state,
			format_datetime(a.creation),
		]
		for a in approvals
	]
	return [
		f"**Pending approvals ({len(approvals)})**",
		markdown_table(["Document", "Type", "State", "Since"], rows),
		"",
	]


def _todos_block(todos):
	"""Markdown lines for open ToDos / assignments."""
	if not todos:
		return ["**Open tasks:** none", ""]
	rows = []
	for t in todos:
		ref = doc_link(t.reference_type, t.reference_name) if t.reference_type and t.reference_name else ""
		text = frappe.utils.strip_html_tags(t.description or "")[:80]
		rows.append([ref or doc_link("ToDo", t.name), text, t.priority or "", str(t.date or "")])
	return [
		f"**Open tasks ({len(todos)})**",
		markdown_table(["Reference", "Task", "Priority", "Due"], rows),
		"",
	]


def workflow_history(doctype, name):
	"""Who moved a document through each workflow state / submit / cancel,
	from its Version log, plus who must act next.

	Parameters:
		doctype (str, required): DocType of the document.
		name (str, required): Document name.

	Returns:
		str: Markdown text.
	"""
	meta = frappe.get_meta(doctype)
	state_field = _workflow_state_field(doctype) or "workflow_state"
	versions = frappe.get_all(
		"Version",
		filters={"ref_doctype": doctype, "docname": name},
		fields=["owner", "creation", "data"],
		order_by="creation asc",
		limit_page_length=200,
	)
	rows = []
	for v in versions:
		rows += _state_changes(v, state_field)
	rows += _workflow_comments(doctype, name)
	rows.sort(key=lambda r: r[3])
	rows = [r[:3] for r in rows]

	lines = [f"**Approval history of {doc_link(doctype, name)}**", ""]
	if rows:
		lines.append(markdown_table(["When", "By", "Change"], rows))
	else:
		lines.append("No workflow or status changes were recorded for this document.")

	pending = (
		frappe.get_all(
			"Workflow Action",
			filters={"reference_doctype": doctype, "reference_name": name, "status": "Open"},
			fields=["name", "workflow_state"],
		)
		if frappe.db.table_exists("Workflow Action")
		else []
	)
	if pending:
		roles = frappe.get_all(
			"Workflow Action Permitted Role",
			filters={"parent": ["in", [p.name for p in pending]]},
			pluck="role",
		)
		lines.append("")
		lines.append(
			f"**Waiting at:** {pending[0].workflow_state} — can be actioned by role(s): {', '.join(sorted(set(roles))) or '-'}"
		)
	if meta.has_field(state_field):
		lines.append(source_line("Version log, Workflow Action", f"{doctype} {name}"))
	return "\n".join(lines)


def _workflow_state_field(doctype):
	"""Field that holds the active workflow's state for a DocType, if any."""
	return frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1}, "workflow_state_field")


def _state_changes(version, state_field):
	"""[[when, by, change]] rows for workflow state / docstatus changes in one Version."""
	try:
		data = json.loads(version.data or "{}")
	except ValueError:
		return []
	labels = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
	rows = []
	for field, old, new in data.get("changed") or []:
		if field == state_field:
			change = f"{old or '-'} → **{new}**"
		elif field == "docstatus":
			change = f"{labels.get(old, old)} → **{labels.get(new, new)}**"
		else:
			continue
		by = frappe.db.get_value("User", version.owner, "full_name") or version.owner
		rows.append([format_datetime(version.creation), by, change, version.creation])
	return rows


def _workflow_comments(doctype, name):
	"""[[when, by, action, sort_key]] from the timeline's Workflow comments,
	which Frappe adds on each workflow transition."""
	comments = frappe.get_all(
		"Comment",
		filters={"reference_doctype": doctype, "reference_name": name, "comment_type": "Workflow"},
		fields=["owner", "creation", "content"],
		order_by="creation asc",
		limit_page_length=100,
	)
	return [
		[
			format_datetime(c.creation),
			frappe.db.get_value("User", c.owner, "full_name") or c.owner,
			f"**{c.content}**",
			c.creation,
		]
		for c in comments
	]


def child_table_rows(doctype, name, tokens):
	"""Rows of the child table(s) the user named ("items", "taxes"), or None
	when no child table was named.

	Parameters:
		doctype (str, required): Parent DocType.
		name (str, required): Parent document name (already permission-checked).
		tokens (list[str], required): Lowercase words of the question.

	Returns:
		str | None: Markdown text.
	"""
	meta = frappe.get_meta(doctype)
	joined = " " + " ".join(tokens) + " "
	tables = list(
		{
			df.options: df for df in meta.fields if df.fieldtype == "Table" and _names_table(df, joined)
		}.values()
	)
	if not tables:
		return None

	doc = frappe.get_doc(doctype, name)
	blocks = []
	for df in tables[:2]:
		blocks.append(_table_block(doc, df))
	return "\n\n".join(blocks)


def _names_table(table_df, joined):
	"""True when the question names this child table by its fieldname:
	"items" / "item", "taxes" / "tax", "payment schedule". Labels are not
	used - custom tables have long labels full of everyday words."""
	phrase = table_df.fieldname.removeprefix("custom_").replace("_", " ")
	variants = {phrase, phrase.rstrip("s"), phrase.removesuffix("es")}
	return any(f" {v} " in joined for v in variants if len(v) > 2)


def _table_block(doc, table_df):
	"""Markdown table of one child table's rows on doc."""
	child_meta = frappe.get_meta(table_df.options)
	preferred = [
		"item_code",
		"item_name",
		"qty",
		"uom",
		"rate",
		"amount",
		"account_head",
		"tax_amount",
		"description",
	]
	fields = [f for f in preferred if child_meta.has_field(f)]
	if len(fields) < 3:
		fields += [df.fieldname for df in child_meta.fields if df.in_list_view and df.fieldname not in fields]
	fields = fields[:6]

	rows = doc.get(table_df.fieldname) or []
	data = mask_record([{f: r.get(f) for f in fields} for r in rows[:50]])
	body = [[_cell(child_meta, f, row.get(f), doc) for f in fields] for row in data]
	header = [child_meta.get_label(f) for f in fields]
	more = f"\n\n_Showing 50 of {len(rows)} rows._" if len(rows) > 50 else ""
	return f"**{table_df.label} ({len(rows)} rows)**\n\n{markdown_table(header, body)}{more}"


def _cell(meta, fieldname, value, doc):
	"""Display text for one child-table cell."""
	df = meta.get_field(fieldname)
	if df and df.fieldtype == "Currency" and value not in (None, ""):
		return fmt_money(value, currency=doc.get("currency"))
	text = frappe.utils.strip_html_tags(str(value)) if value not in (None, "") else ""
	return text[:80]


def stock_balance(item_codes):
	"""Warehouse-wise stock of items from Bin, via get_list.

	Parameters:
		item_codes (list[str], required): Item codes to report.

	Returns:
		str: Markdown text.
	"""
	if not frappe.has_permission("Bin", "read"):
		return "You do not have permission to see stock balances (Bin)."
	bins = frappe.get_list(
		"Bin",
		filters={"item_code": ["in", item_codes]},
		fields=["item_code", "warehouse", "actual_qty", "reserved_qty", "ordered_qty", "projected_qty"],
		order_by="item_code asc, actual_qty desc",
		limit_page_length=MAX_ROWS * 2,
	)
	if not bins:
		return f"No stock found for {', '.join(item_codes[:5])}."
	rows = [
		[
			doc_link("Item", b.item_code),
			b.warehouse,
			flt(b.actual_qty),
			flt(b.reserved_qty),
			flt(b.ordered_qty),
			flt(b.projected_qty),
		]
		for b in bins
	]
	header = ["Item", "Warehouse", "Actual", "Reserved", "Ordered", "Projected"]
	return f"**Stock balance**\n\n{markdown_table(header, rows)}\n{source_line('Bin', 'item = ' + ', '.join(item_codes[:3]))}"


def outstanding_summary(party_type, party_names=None, company=None):
	"""Unpaid submitted invoices grouped by party - like Accounts Payable /
	Receivable, but from invoice outstanding amounts.

	Parameters:
		party_type (str, required): "Supplier" or "Customer".
		party_names (list[str], optional): Limit to these parties.
		company (str, optional): Limit to one company.

	Returns:
		str: Markdown text.
	"""
	doctype = "Purchase Invoice" if party_type == "Supplier" else "Sales Invoice"
	field = "supplier" if party_type == "Supplier" else "customer"
	if not frappe.has_permission(doctype, "read"):
		return f"You do not have permission to read {doctype}."

	filters = {"docstatus": 1, "outstanding_amount": ["!=", 0]}
	if party_names:
		filters[field] = ["in", party_names]
	if company:
		filters["company"] = company
	rows = frappe.get_list(
		doctype,
		filters=filters,
		fields=[field, {"SUM": "outstanding_amount", "as": "outstanding"}, {"COUNT": "*", "as": "invoices"}],
		group_by=field,
		order_by="outstanding desc",
		limit_page_length=MAX_ROWS,
	)
	title = "Accounts Payable" if party_type == "Supplier" else "Accounts Receivable"
	if not rows:
		return f"No outstanding {doctype}s found."
	total = sum(flt(r.outstanding) for r in rows)
	title_field = f"{field}_name"
	names = dict(
		frappe.get_all(
			party_type,
			filters={"name": ["in", [r[field] for r in rows]]},
			fields=["name", title_field],
			as_list=True,
		)
	)
	body = [
		[doc_link(party_type, r[field]), names.get(r[field]) or "", r.invoices, fmt_money(r.outstanding)]
		for r in rows
	]
	return (
		f"**{title} — outstanding by {party_type.lower()}** (total {fmt_money(total)})\n\n"
		f"{markdown_table([party_type, 'Name', 'Invoices', 'Outstanding'], body)}\n"
		f"{source_line(doctype, 'submitted, outstanding ≠ 0')}"
	)


def sum_field(meta, tokens):
	"""Currency field the user wants summed ("total outstanding" -> outstanding_amount)."""
	for t in tokens:
		if t in ("outstanding", "paid", "tax", "discount", "net"):
			candidates = [
				df.fieldname
				for df in meta.fields
				if df.fieldtype == "Currency"
				and t in df.fieldname.split("_")
				and not df.fieldname.startswith("base_")
			]
			if candidates:
				return min(candidates, key=len)
	return "grand_total" if meta.has_field("grand_total") else None


def wants_sum(tokens):
	"""True for "sum of ..." / "total amount of ..." questions."""
	joined = " " + " ".join(tokens) + " "
	return any(f" {p} " in joined for p in SUM_PHRASES)
