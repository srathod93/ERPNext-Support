// Copyright (c) 2026, shyam8848 and contributors
// For license information, please see license.txt

// Adds an "Ask Support AI why" button to error dialogs shown on a form
// (validation errors on save/submit), which opens the chat with the
// document and the error text already loaded.
frappe.provide("support_ai");

$(document).on("app_ready", () => {
	if (frappe.session.user === "Guest" || support_ai.error_help_installed) return;
	support_ai.error_help_installed = true;

	const original_msgprint = frappe.msgprint;
	frappe.msgprint = function (msg, title) {
		const dialog = original_msgprint.apply(this, arguments);
		try {
			support_ai.add_error_help(dialog, msg, title);
		} catch (e) {
			// The helper must never break Frappe's own error dialog.
			console.warn("Support AI error help:", e);
		}
		return dialog;
	};
});

/**
 * Adds the button when the dialog is a red (error) message on a saved form.
 *
 * @param {frappe.ui.Dialog} dialog - The message dialog frappe.msgprint returned.
 * @param {string|object} msg - What was passed to frappe.msgprint.
 * @param {string} title - Optional title argument.
 */
support_ai.add_error_help = function (dialog, msg, title) {
	if (!dialog || !dialog.custom_actions || !support_ai.widget) return;
	if (!window.cur_frm || cur_frm.is_new() || !cur_frm.doc || !cur_frm.doc.name) return;

	const data = $.isPlainObject(msg) ? msg : typeof msg === "string" && msg[0] === "{" ? JSON.parse(msg) : { message: msg, title };
	if (data.indicator !== "red") return;

	dialog.custom_actions.find(".support-ai-why").remove();
	const text = $("<div>").html(data.message || "").text().trim().slice(0, 1000);
	const doctype = cur_frm.doctype;
	const name = cur_frm.doc.name;
	dialog.add_custom_action(
		__("Ask Support AI why"),
		() => {
			dialog.hide();
			support_ai.widget.ask_about_error(doctype, name, text);
		},
		"support-ai-why btn-default btn-sm"
	);
};
