frappe.ui.form.on("Support AI Bot", {
	refresh(frm) {
		frm.add_custom_button(__("Preview Instructions"), () =>
			support_ai.preview_instruction(frm.doc.instruction, frm.doc.dynamic_instructions)
		);

		if (frm.is_new()) return;

		frm.add_custom_button(__("Test Bot"), () => {
			if (frm.is_dirty()) return frappe.msgprint(__("Save the bot first."));
			frappe.call({
				method: "support_ai.support_ai.doctype.support_ai_bot.support_ai_bot.test_bot",
				args: { bot: frm.doc.name },
				freeze: true,
				freeze_message: __("Contacting the model..."),
				callback: (r) => support_ai.show_test_result(r),
			});
		});

		frm.add_custom_button(__("Fetch Models"), () => {
			if (frm.is_dirty()) return frappe.msgprint(__("Save the bot first."));
			support_ai.show_models_dialog({ bot: frm.doc.name }, (model) => frm.set_value("model", model));
		});

		if (frm.doc.enable_file_search && frm.doc.engine === "OpenAI Agents SDK") {
			frm.add_custom_button(__("Sync Knowledge to OpenAI"), () =>
				frappe.call({
					method: "support_ai.support_ai.doctype.support_ai_bot.support_ai_bot.sync_knowledge_base",
					args: { bot: frm.doc.name },
					freeze: true,
					freeze_message: __("Uploading files..."),
					callback: () => frm.reload_doc(),
				})
			);
		}

		if (frm.doc.use_google_document_ai) {
			frm.add_custom_button(__("Select Document AI Processor"), () =>
				support_ai.show_processors_dialog((id) => frm.set_value("google_processor_id", id))
			);
		}
	},

	instruction_template(frm) {
		if (!frm.doc.instruction_template) return;
		frappe.db
			.get_value("Support AI Instruction Template", frm.doc.instruction_template, ["instruction", "dynamic_instructions"])
			.then(({ message }) => {
				if (!message) return;
				const apply = () => {
					frm.set_value("instruction", message.instruction);
					frm.set_value("dynamic_instructions", message.dynamic_instructions);
				};
				if (frm.doc.instruction && frm.doc.instruction !== message.instruction) {
					frappe.confirm(__("Replace the current instruction with the template?"), apply);
				} else {
					apply();
				}
			});
	},
});
