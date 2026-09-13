frappe.pages["support-ai"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Support AI",
		single_column: true,
	});
	new support_ai.Chat(page);
};

frappe.provide("support_ai");

support_ai.Chat = class {
	constructor(page) {
		this.page = page;
		this.conversation = null;
		this.reference_doctype = frappe.route_options?.reference_doctype || null;
		this.reference_name = frappe.route_options?.reference_name || null;
		frappe.route_options = null;
		this.make();
		this.refresh_conversations();
		if (this.reference_doctype && this.reference_name) {
			this.start_new_conversation();
		}
	}

	make() {
		this.page.main.html(`
			<div class="support-ai-wrapper" style="display:flex; gap: 16px; height: calc(100vh - 200px);">
				<div class="support-ai-sidebar" style="width: 260px; flex-shrink:0; overflow-y:auto; border-right: 1px solid var(--border-color); padding-right: 10px;">
					<button class="btn btn-primary btn-sm btn-block support-ai-new">+ New Conversation</button>
					<div class="support-ai-context-note text-muted small" style="margin: 8px 0;"></div>
					<div class="support-ai-conversation-list" style="margin-top: 10px;"></div>
				</div>
				<div class="support-ai-main" style="flex:1; display:flex; flex-direction:column; min-width:0;">
					<div class="support-ai-messages" style="flex:1; overflow-y:auto; padding: 10px; background: var(--subtle-fg); border-radius: 6px;"></div>
					<div class="support-ai-input-row" style="display:flex; gap: 8px; margin-top: 10px;">
						<textarea class="form-control support-ai-input" rows="2" placeholder="Describe the issue, e.g. 'Sales Invoice INV-001 won't submit, what is wrong?'"></textarea>
						<button class="btn btn-primary support-ai-send">Ask</button>
					</div>
				</div>
			</div>
		`);

		this.$sidebar_list = this.page.main.find(".support-ai-conversation-list");
		this.$messages = this.page.main.find(".support-ai-messages");
		this.$input = this.page.main.find(".support-ai-input");
		this.$context_note = this.page.main.find(".support-ai-context-note");

		this.page.main.find(".support-ai-new").on("click", () => this.start_new_conversation());
		this.page.main.find(".support-ai-send").on("click", () => this.send());
		this.$input.on("keydown", (e) => {
			if (e.key === "Enter" && !e.shiftKey) {
				e.preventDefault();
				this.send();
			}
		});

		this.update_context_note();
	}

	update_context_note() {
		if (this.reference_doctype && this.reference_name) {
			this.$context_note.html(
				`Context: <a href="/app/${frappe.router.slug(this.reference_doctype)}/${this.reference_name}" target="_blank">${this.reference_doctype} ${this.reference_name}</a>`
			);
		} else {
			this.$context_note.html("No document context. Ask a general ERPNext/Frappe question, or open this from a document.");
		}
	}

	start_new_conversation() {
		frappe.call({
			method: "support_ai.api.new_conversation",
			args: {
				reference_doctype: this.reference_doctype,
				reference_name: this.reference_name,
			},
			callback: (r) => {
				this.conversation = r.message;
				this.$messages.empty();
				this.render_message("Assistant", this.reference_doctype && this.reference_name
					? `I have the context for ${this.reference_doctype} ${this.reference_name} loaded. What issue are you facing?`
					: "Hi, I'm Support AI. Describe the issue you're facing in ERPNext/Frappe, or open me from a specific document for targeted diagnosis.");
				this.refresh_conversations();
			},
		});
	}

	refresh_conversations() {
		frappe.call({
			method: "support_ai.api.list_conversations",
			callback: (r) => {
				this.$sidebar_list.empty();
				(r.message || []).forEach((c) => {
					const $item = $(`
						<div class="support-ai-conversation-item" style="padding:6px 8px; cursor:pointer; border-radius:4px; font-size: 12px;">
							<div style="font-weight:500;">${frappe.utils.escape_html(c.title || c.name)}</div>
							<div class="text-muted">${c.status || ""}</div>
						</div>
					`);
					$item.on("click", () => this.load_conversation(c.name));
					this.$sidebar_list.append($item);
				});
			},
		});
	}

	load_conversation(name) {
		frappe.call({
			method: "support_ai.api.get_conversation",
			args: { name },
			callback: (r) => {
				const convo = r.message;
				this.conversation = convo.name;
				this.reference_doctype = convo.reference_doctype;
				this.reference_name = convo.reference_name;
				this.update_context_note();
				this.$messages.empty();
				(convo.messages || []).forEach((m) => this.render_message(m.role, m.content));
			},
		});
	}

	send() {
		const message = (this.$input.val() || "").trim();
		if (!message) return;
		if (!this.conversation) {
			frappe.msgprint(__("Start a new conversation first."));
			return;
		}
		this.render_message("User", message);
		this.$input.val("");

		const $pending = this.render_message("Assistant", `<span class="text-muted">Thinking...</span>`, true);

		frappe.call({
			method: "support_ai.api.ask",
			args: {
				conversation: this.conversation,
				message,
				reference_doctype: this.reference_doctype,
				reference_name: this.reference_name,
			},
			callback: (r) => {
				$pending.remove();
				this.render_message("Assistant", r.message.reply);
				this.refresh_conversations();
			},
			error: () => {
				$pending.remove();
				this.render_message("Assistant", "⚠️ Something went wrong. Please try again.");
			},
		});
	}

	render_message(role, content, is_html) {
		const is_user = role === "User";
		const $msg = $(`
			<div class="support-ai-message" style="display:flex; ${is_user ? "justify-content:flex-end;" : ""} margin-bottom: 10px;">
				<div style="max-width: 75%; padding: 8px 12px; border-radius: 8px; background: ${is_user ? "var(--primary)" : "var(--fg-color)"}; color: ${is_user ? "var(--btn-primary-color)" : "var(--text-color)"}; border: 1px solid var(--border-color); white-space: pre-wrap;"></div>
			</div>
		`);
		const $bubble = $msg.find("div div");
		if (is_html) {
			$bubble.html(content);
		} else {
			$bubble.text(content);
		}
		this.$messages.append($msg);
		this.$messages.scrollTop(this.$messages[0].scrollHeight);
		return $msg;
	}
};
