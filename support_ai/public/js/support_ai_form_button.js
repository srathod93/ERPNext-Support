frappe.provide("support_ai");

frappe.ui.form.on("*", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.name) return;

		frm.page.add_menu_item(__("Ask Support AI"), () => {
			if (support_ai.widget) {
				support_ai.widget.toggle(true);
			} else {
				frappe.route_options = {
					reference_doctype: frm.doctype,
					reference_name: frm.doc.name,
				};
				frappe.set_route("support-ai");
			}
		});
	},
});

// --- Shared admin helpers (Support AI Settings / Support AI Bot forms) -----------

support_ai.show_models_dialog = function (args, on_pick) {
	frappe.call({
		method: "support_ai.support_ai.doctype.support_ai_settings.support_ai_settings.get_available_models",
		args,
		freeze: true,
		freeze_message: __("Fetching models..."),
		callback: (r) => {
			const res = r.message || {};
			if (!res.success) {
				frappe.msgprint({ title: __("Could not list models"), indicator: "red", message: frappe.utils.escape_html(res.message || "") });
				return;
			}
			if (!res.models.length) {
				frappe.msgprint(__("{0} returned no models. Pull/download one first.", [res.provider]));
				return;
			}
			const d = new frappe.ui.Dialog({
				title: __("Models on {0}", [res.provider]),
				fields: [{ fieldtype: "HTML", fieldname: "list" }],
			});
			const $list = $(`<div class="list-group"></div>`);
			res.models.forEach((model) => {
				$(`<a class="list-group-item list-group-item-action" style="cursor:pointer"></a>`)
					.text(model)
					.on("click", () => {
						on_pick(model);
						d.hide();
					})
					.appendTo($list);
			});
			d.fields_dict.list.$wrapper.append($list);
			d.show();
		},
	});
};

support_ai.show_test_result = function (r, provider) {
	const res = r.message || {};
	if (res.success) {
		frappe.msgprint({
			title: __("Connected"),
			indicator: "green",
			message: __("{0} / {1} replied: {2}", [res.provider, res.model, frappe.utils.escape_html(res.reply)]),
		});
	} else {
		frappe.msgprint({
			title: __("Connection Failed"),
			indicator: "red",
			message: frappe.utils.escape_html(res.message || __("Unknown error")),
		});
	}
};

support_ai.preview_instruction = function (instruction, dynamic) {
	frappe.call({
		method: "support_ai.ai.instructions.get_instruction_preview",
		args: { instruction: instruction || "", dynamic_instructions: dynamic ? 1 : 0 },
		callback: (r) => {
			frappe.msgprint({
				title: __("Instruction Preview (as you)"),
				message: `<pre style="white-space:pre-wrap">${frappe.utils.escape_html(r.message || "")}</pre>`,
				wide: true,
			});
		},
	});
};

support_ai.show_processors_dialog = function (on_pick) {
	const base = "support_ai.ai.google_docai.";
	frappe.call({
		method: base + "list_processors",
		freeze: true,
		callback: (r) => {
			const processors = r.message || [];
			const d = new frappe.ui.Dialog({
				title: __("Google Document AI Processors"),
				fields: [{ fieldtype: "HTML", fieldname: "list" }],
				primary_action_label: __("Create Processor"),
				primary_action: () => {
					d.hide();
					frappe.call({
						method: base + "get_processor_types",
						callback: (t) => {
							const types = t.message || {};
							frappe.prompt(
								{
									fieldname: "processor_type",
									fieldtype: "Select",
									label: __("Processor Type"),
									options: Object.keys(types).join("\n"),
									description: __("Creating a processor is billed by Google Cloud."),
									reqd: 1,
								},
								(v) =>
									frappe.call({
										method: base + "create_processor",
										args: v,
										freeze: true,
										callback: () => support_ai.show_processors_dialog(on_pick),
									}),
								__("Create Processor")
							);
						},
					});
				},
			});
			const $list = $(`<div></div>`);
			if (!processors.length) $list.append(`<p class="text-muted">${__("No processors yet.")}</p>`);
			processors.forEach((p) => {
				const $row = $(`
					<div class="d-flex align-items-center justify-content-between" style="padding:6px 0;border-bottom:1px solid var(--border-color)">
						<div><b></b><div class="text-muted small"></div></div>
						<div>
							${on_pick ? `<button class="btn btn-xs btn-primary sai-use">${__("Use")}</button>` : ""}
							<button class="btn btn-xs btn-danger sai-del">${__("Delete")}</button>
						</div>
					</div>`);
				$row.find("b").text(p.display_name);
				$row.find(".small").text(`${p.id} · ${p.type} · ${p.state}`);
				$row.find(".sai-use").on("click", () => {
					on_pick(p.id);
					d.hide();
				});
				$row.find(".sai-del").on("click", () =>
					frappe.confirm(__("Permanently delete processor {0}?", [p.display_name]), () =>
						frappe.call({
							method: base + "delete_processor",
							args: { processor_id: p.id },
							freeze: true,
							callback: () => {
								d.hide();
								support_ai.show_processors_dialog(on_pick);
							},
						})
					)
				);
				$list.append($row);
			});
			d.fields_dict.list.$wrapper.append($list);
			d.show();
		},
	});
};
