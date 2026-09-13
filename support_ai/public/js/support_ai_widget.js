frappe.provide("support_ai");

const SUPPORT_AI_BUBBLE_SVG = `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
	<path d="M4 4h16v12H7l-3 3V4z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" fill="none"/>
</svg>`;

support_ai.Widget = class {
	constructor() {
		this.conversation = null;
		this.is_open = false;
		this.reference_doctype = null;
		this.reference_name = null;
		this.make();
	}

	make() {
		this.$bubble = $(`<div class="support-ai-bubble" title="${__("Ask Support AI")}">${SUPPORT_AI_BUBBLE_SVG}</div>`).appendTo(
			"body"
		);

		this.$panel = $(`
			<div class="support-ai-panel">
				<div class="support-ai-panel-header">
					<div class="support-ai-title">${__("Support AI")}</div>
					<div class="support-ai-context"></div>
					<button class="support-ai-new-btn" title="${__("New Conversation")}">+</button>
					<button class="support-ai-close-btn" title="${__("Close")}">&times;</button>
				</div>
				<div class="support-ai-messages"></div>
				<div class="support-ai-input-row">
					<textarea rows="2" placeholder="${__("Describe the issue, e.g. 'this invoice won't submit, what's wrong?'")}"></textarea>
					<button class="btn btn-primary btn-sm support-ai-send-btn">${__("Ask")}</button>
				</div>
			</div>
		`).appendTo("body");

		this.$messages = this.$panel.find(".support-ai-messages");
		this.$context = this.$panel.find(".support-ai-context");
		this.$input = this.$panel.find("textarea");

		this.$bubble.on("click", () => this.toggle());
		this.$panel.find(".support-ai-close-btn").on("click", () => this.toggle(false));
		this.$panel.find(".support-ai-new-btn").on("click", () => this.start_new_conversation());
		this.$panel.find(".support-ai-send-btn").on("click", () => this.send());
		this.$input.on("keydown", (e) => {
			if (e.key === "Enter" && !e.shiftKey) {
				e.preventDefault();
				this.send();
			}
		});
	}

	toggle(force) {
		this.is_open = force !== undefined ? force : !this.is_open;
		this.$panel.toggleClass("support-ai-panel-open", this.is_open);
		if (this.is_open) {
			this.sync_with_current_document();
			this.$input.trigger("focus");
		}
	}

	get_current_doc_reference() {
		if (typeof cur_frm !== "undefined" && cur_frm && cur_frm.doc && !cur_frm.is_new()) {
			return { doctype: cur_frm.doctype, name: cur_frm.doc.name };
		}
		return null;
	}

	sync_with_current_document() {
		const ref = this.get_current_doc_reference();
		const new_dt = ref ? ref.doctype : null;
		const new_dn = ref ? ref.name : null;
		const changed = new_dt !== this.reference_doctype || new_dn !== this.reference_name;

		this.reference_doctype = new_dt;
		this.reference_name = new_dn;
		this.update_context_label();

		if (changed || !this.conversation) {
			this.start_new_conversation();
		}
	}

	update_context_label() {
		if (this.reference_doctype && this.reference_name) {
			this.$context.text(`${this.reference_doctype}: ${this.reference_name}`);
		} else {
			this.$context.text(__("General question"));
		}
	}

	start_new_conversation() {
		this.$messages.empty();
		frappe.call({
			method: "support_ai.api.new_conversation",
			args: {
				reference_doctype: this.reference_doctype,
				reference_name: this.reference_name,
			},
			callback: (r) => {
				this.conversation = r.message;
				const greeting =
					this.reference_doctype && this.reference_name
						? __("I have the context for {0} {1} loaded. What issue are you facing?", [
								this.reference_doctype,
								this.reference_name,
						  ])
						: __("Hi, I'm Support AI. Describe the issue you're facing, or open a document first for a targeted diagnosis.");
				this.render_message("Assistant", greeting);
			},
		});
	}

	send() {
		const message = (this.$input.val() || "").trim();
		if (!message) return;
		if (!this.conversation) {
			frappe.show_alert({ message: __("Starting a new conversation, please retry in a moment."), indicator: "orange" });
			this.start_new_conversation();
			return;
		}

		this.render_message("User", message);
		this.$input.val("");

		const started_at = Date.now();
		const $pending = this.render_message("Assistant", __("Thinking... (0s)"));
		const $pendingText = $pending.find(".support-ai-msg-bubble");
		const timer = setInterval(() => {
			$pendingText.text(__("Thinking... ({0}s)", [Math.round((Date.now() - started_at) / 1000)]));
		}, 1000);

		frappe.call({
			method: "support_ai.api.ask",
			args: {
				conversation: this.conversation,
				message,
				reference_doctype: this.reference_doctype,
				reference_name: this.reference_name,
			},
			callback: (r) => {
				clearInterval(timer);
				$pending.remove();
				this.render_message("Assistant", r.message.reply);
			},
			error: () => {
				clearInterval(timer);
				$pending.remove();
				this.render_message("Assistant", __("Something went wrong reaching Support AI. Please try again."));
			},
		});
	}

	render_message(role, content) {
		const is_user = role === "User";
		const $row = $(`
			<div class="support-ai-msg-row ${is_user ? "support-ai-msg-user" : "support-ai-msg-assistant"}">
				<div class="support-ai-msg-bubble"></div>
			</div>
		`);
		$row.find(".support-ai-msg-bubble").text(content);
		this.$messages.append($row);
		this.$messages.scrollTop(this.$messages[0].scrollHeight);
		return $row;
	}
};

$(document).on("app_ready", () => {
	if (frappe.session.user === "Guest") return;
	if (!support_ai.widget) {
		support_ai.widget = new support_ai.Widget();
	}
});
