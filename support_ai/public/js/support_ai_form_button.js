frappe.ui.form.on("*", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.name) return;

		frm.page.add_menu_item(__("Ask Support AI"), () => {
			frappe.route_options = {
				reference_doctype: frm.doctype,
				reference_name: frm.doc.name,
			};
			frappe.set_route("support-ai");
		});
	},
});
