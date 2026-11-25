# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Scheduled jobs for Support AI. Wired in hooks.py scheduler_events."""

import frappe

from support_ai.diagnostics.insights import attention_summary

MAX_DIGEST_USERS = 500


def send_daily_digest():
	"""Emails each enabled user with the digest role a "what needs your
	attention" summary (pending approvals, open tasks, unread notifications),
	built as that user so only what they may see is included. Users with
	nothing pending get no email."""
	settings = frappe.get_single("Support AI Settings")
	if not settings.enable_daily_digest or not settings.digest_role:
		return

	for user in _digest_users(settings.digest_role):
		try:
			_send_digest_to(user)
		except Exception:
			frappe.log_error(
				title=f"Support AI: daily digest failed for {user}", message=frappe.get_traceback()
			)
		finally:
			frappe.set_user("Administrator")


def _digest_users(role):
	"""Enabled system users who have the digest role."""
	users = frappe.get_all(
		"Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent", distinct=True
	)
	return frappe.get_all(
		"User",
		filters={"name": ["in", users], "enabled": 1, "user_type": "System User"},
		pluck="name",
		limit_page_length=MAX_DIGEST_USERS,
	)


def _send_digest_to(user):
	"""Builds and emails one user's digest, if anything needs attention."""
	frappe.set_user(user)
	summary = attention_summary(user)
	if not any(summary["counts"].values()):
		return

	counts = summary["counts"]
	subject = f"Support AI: {counts['approvals']} approvals, {counts['todos']} tasks waiting for you"
	# Chat links are relative (/app/...); email needs absolute URLs.
	html = frappe.utils.md_to_html(summary["text"].replace("](/app/", f"]({frappe.utils.get_url()}/app/"))
	frappe.sendmail(
		recipients=[user], subject=subject, message=html, reference_doctype="User", reference_name=user
	)
