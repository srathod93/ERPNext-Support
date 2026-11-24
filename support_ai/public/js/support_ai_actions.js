// Copyright (c) 2026, shyam8848 and contributors
// For license information, please see license.txt

// One-click actions under an assistant answer: suggestion chips, "Open
// list" / "Export to CSV" buttons, Confirm/Cancel cards for data changes the
// AI proposed, and 👍 / 👎 feedback.
frappe.provide("support_ai");

const API = "support_ai.support_ai.api.v1.assistant.";

support_ai.ActionRenderer = class {
	/**
	 * @param {object} widget - The owning support_ai.Widget.
	 */
	constructor(widget) {
		this.widget = widget;
	}

	/**
	 * Builds the actions row and feedback buttons for one assistant message.
	 *
	 * @param {object} opts - Message options: actions, index, feedback, answered_by.
	 * @returns {jQuery} Element to append under the message text.
	 */
	render(opts) {
		const $wrap = $(`<div class="support-ai-actions-area"></div>`);
		const actions = opts.actions || [];
		const chips = actions.filter((a) => a.type === "suggestion");
		const buttons = actions.filter((a) => a.type === "open_list" || a.type === "export");

		if (chips.length) $wrap.append(this.chips(chips));
		if (buttons.length) $wrap.append(this.buttons(buttons));
		actions.filter((a) => a.type === "confirm_write").forEach((a) => $wrap.append(this.confirm_card(a)));
		if (opts.index !== undefined && opts.index !== null && opts.answered_by) {
			$wrap.append(this.feedback(opts.index, opts.feedback));
		}
		return $wrap;
	}

	/**
	 * Clickable question chips; clicking sends the prompt.
	 *
	 * @param {Array<{label: string, prompt: string}>} items - Chips to show.
	 * @returns {jQuery} Chip row.
	 */
	chips(items) {
		const $row = $(`<div class="support-ai-suggestions"></div>`);
		items.forEach((item) => {
			$(`<button class="support-ai-suggestion"></button>`)
				.text(item.label)
				.attr("title", item.prompt)
				.on("click", () => this.widget.send_text(item.prompt))
				.appendTo($row);
		});
		return $row;
	}

	/**
	 * "Open list" and "Export to CSV" buttons.
	 *
	 * @param {object[]} items - open_list / export actions.
	 * @returns {jQuery} Button row.
	 */
	buttons(items) {
		const $row = $(`<div class="support-ai-action-buttons"></div>`);
		items.forEach((a) => {
			const $btn = $(`<button class="btn btn-xs btn-default"></button>`).text(a.label).appendTo($row);
			$btn.on("click", () => (a.type === "open_list" ? this.open_list(a) : this.export_csv(a, $btn)));
		});
		return $row;
	}

	/**
	 * Opens the DocType list view with the answer's filters applied.
	 *
	 * @param {{doctype: string, filters: object}} action - open_list action.
	 */
	open_list(action) {
		frappe.route_options = action.filters || {};
		frappe.set_route("List", action.doctype);
	}

	/**
	 * Downloads all rows matching the answer's filters (up to 5000) as CSV.
	 *
	 * @param {{doctype: string, filters: object, fields: string[]}} action - export action.
	 * @param {jQuery} $btn - Button to disable while loading.
	 */
	export_csv(action, $btn) {
		$btn.prop("disabled", true);
		frappe.call({
			method: API + "export_records",
			args: {
				doctype: action.doctype,
				filters: JSON.stringify(action.filters || {}),
				fields: JSON.stringify(action.fields || ["name"]),
			},
			callback: (r) => {
				const res = r.message || { columns: [], rows: [] };
				const csv = [res.columns, ...res.rows].map((row) => row.map(csv_cell).join(",")).join("\n");
				const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
				const link = document.createElement("a");
				link.href = URL.createObjectURL(blob);
				link.download = `${frappe.scrub(action.doctype)}_${frappe.datetime.now_date()}.csv`;
				link.click();
				URL.revokeObjectURL(link.href);
			},
			always: () => $btn.prop("disabled", false),
		});
	}

	/**
	 * Confirm / Cancel card for a data change the AI proposed. Nothing is
	 * written until the user clicks Confirm.
	 *
	 * @param {{action_id: string, label: string, summary: string}} action - confirm_write action.
	 * @returns {jQuery} Card element.
	 */
	confirm_card(action) {
		const $card = $(`
			<div class="support-ai-confirm-card">
				<div class="support-ai-confirm-title">${__("Confirm this change?")}</div>
				<div class="support-ai-confirm-summary"></div>
				<div class="support-ai-confirm-buttons">
					<button class="btn btn-xs btn-primary support-ai-confirm-yes">${__("Confirm")}</button>
					<button class="btn btn-xs btn-default support-ai-confirm-no">${__("Cancel")}</button>
				</div>
			</div>
		`);
		$card.find(".support-ai-confirm-summary").text(action.summary || action.label);
		const resolve = (confirm) => {
			$card.find("button").prop("disabled", true);
			frappe.call({
				method: API + "resolve_write_action",
				args: { action_id: action.action_id, confirm: confirm ? 1 : 0 },
				callback: (r) => {
					$card.find(".support-ai-confirm-buttons").replaceWith(
						$(`<div class="text-muted small"></div>`).text(confirm ? __("Confirmed") : __("Cancelled"))
					);
					this.widget.stored_count = r.message.message_index + 1;
					this.widget.render_message("Assistant", r.message.reply, { index: r.message.message_index });
				},
				error: () => $card.find("button").prop("disabled", false),
			});
		};
		$card.find(".support-ai-confirm-yes").on("click", () => resolve(true));
		$card.find(".support-ai-confirm-no").on("click", () => resolve(false));
		return $card;
	}

	/**
	 * 👍 / 👎 buttons; 👎 asks what was wrong. Saved for the usage report.
	 *
	 * @param {number} index - Message index in the conversation.
	 * @param {string} current - "Up", "Down" or empty.
	 * @returns {jQuery} Feedback row.
	 */
	feedback(index, current) {
		const $row = $(`
			<div class="support-ai-feedback">
				<button class="support-ai-feedback-btn" data-value="Up" title="${__("Good answer")}">👍</button>
				<button class="support-ai-feedback-btn" data-value="Down" title="${__("Wrong or unhelpful")}">👎</button>
			</div>
		`);
		const mark = (value) => {
			$row.find(".support-ai-feedback-btn").removeClass("active");
			if (value) $row.find(`[data-value="${value}"]`).addClass("active");
		};
		mark(current);
		const save = (value, comment) =>
			frappe.call({
				method: API + "set_feedback",
				args: { conversation: this.widget.conversation, message_index: index, feedback: value, comment },
				callback: () => mark(value),
			});
		$row.find(".support-ai-feedback-btn").on("click", (e) => {
			const value = $(e.currentTarget).data("value");
			if (value === "Down") {
				frappe.prompt(
					{ fieldname: "comment", fieldtype: "Small Text", label: __("What was wrong? (optional)") },
					(v) => save(value, v.comment),
					__("Thanks for the feedback"),
					__("Send")
				);
			} else {
				save(value);
			}
		});
		return $row;
	}
};

/**
 * Quotes one CSV cell when needed.
 *
 * @param {*} value - Cell value.
 * @returns {string} CSV-safe text.
 */
function csv_cell(value) {
	const text = value === null || value === undefined ? "" : String(value);
	return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}
