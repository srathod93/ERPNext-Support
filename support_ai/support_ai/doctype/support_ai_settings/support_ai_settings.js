const SUPPORT_AI_MODEL_FIELDS = {
	Ollama: "ollama_model",
	OpenAI: "openai_model",
	"OpenAI Compatible": "api_model",
	"LM Studio": "lm_studio_model",
	LocalAI: "localai_model",
};

frappe.ui.form.on("Support AI Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => {
			if (frm.is_dirty()) return frappe.msgprint(__("Save the settings first."));
			frappe.call({
				method: "support_ai.support_ai.doctype.support_ai_settings.support_ai_settings.test_connection",
				freeze: true,
				freeze_message: __("Contacting {0}...", [frm.doc.llm_provider]),
				callback: (r) => support_ai.show_test_result(r),
			});
		});

		frm.add_custom_button(__("Fetch Available Models"), () => {
			if (frm.is_dirty()) return frappe.msgprint(__("Save the settings first."));
			frappe.prompt(
				{
					fieldname: "provider",
					fieldtype: "Select",
					label: __("Provider"),
					options: Object.keys(SUPPORT_AI_MODEL_FIELDS).join("\n"),
					default: frm.doc.llm_provider,
				},
				({ provider }) =>
					support_ai.show_models_dialog({ provider }, (model) => {
						frm.set_value(SUPPORT_AI_MODEL_FIELDS[provider], model);
					}),
				__("List models from")
			);
		});

		if (frm.doc.enable_google_document_ai) {
			frm.add_custom_button(__("Document AI Processors"), () => {
				if (frm.is_dirty()) return frappe.msgprint(__("Save the settings first."));
				support_ai.show_processors_dialog((id) => frm.set_value("google_default_processor_id", id));
			});
		}
	},
});
