frappe.ui.form.on("Support AI Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => {
			frappe.call({
				method: "support_ai.support_ai.doctype.support_ai_settings.support_ai_settings.test_connection",
				freeze: true,
				freeze_message: __("Contacting {0}...", [frm.doc.llm_provider]),
				callback: (r) => {
					if (r.message && r.message.success) {
						frappe.msgprint({
							title: __("Connected"),
							indicator: "green",
							message: __("Reply: {0}", [frappe.utils.escape_html(r.message.reply)]),
						});
					} else {
						frappe.msgprint({
							title: __("Connection Failed"),
							indicator: "red",
							message: frappe.utils.escape_html((r.message && r.message.message) || __("Unknown error")),
						});
					}
				},
			});
		});
	},
});
