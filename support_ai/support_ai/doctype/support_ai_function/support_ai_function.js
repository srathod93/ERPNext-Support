frappe.ui.form.on("Support AI Function", {
	refresh(frm) {
		if (frm.doc.requires_write_permissions) {
			frm.dashboard.set_headline(
				__("This function changes data. It only runs on bots with 'Allow Write Actions' and for users with the Support AI write role (see Support AI Settings)."),
				"orange"
			);
		}
		frm.set_query("reference_doctype", () => ({ filters: { istable: 0, issingle: 0 } }));
	},
});
