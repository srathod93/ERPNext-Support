frappe.ui.form.on("Support AI Instruction Template", {
	refresh(frm) {
		frm.add_custom_button(__("Preview"), () =>
			support_ai.preview_instruction(frm.doc.instruction, frm.doc.dynamic_instructions)
		);
	},
});
