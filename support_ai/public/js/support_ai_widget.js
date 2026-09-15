frappe.provide("support_ai");

const SUPPORT_AI_BUBBLE_SVG = `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
	<path d="M4 4h16v12H7l-3 3V4z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" fill="none"/>
</svg>`;

// Real inline SVGs rather than Unicode glyphs (▢ ❐ ▣ etc.) - those rely on
// the OS/browser having a glyph for that exact codepoint and were observed
// to render as blank in the header, which is why the buttons "disappeared".
const ICONS = {
	history: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 3.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
	plus: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`,
	minimize: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="5" y1="19" x2="19" y2="19"/></svg>`,
	maximize: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="5" y="5" width="14" height="14" rx="1.5"/></svg>`,
	restore: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="7" y="7" width="11" height="11" rx="1.2"/><path d="M7 4.5h9.5A2 2 0 0 1 18.5 6.5V16" stroke-linecap="round"/></svg>`,
	close: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>`,
	edit: `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>`,
	search: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65" stroke-linecap="round"/></svg>`,
	back: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>`,
};

const DEFAULT_WIDTH = 400;
const DEFAULT_HEIGHT = 560;
const MINIMIZED_HEIGHT = 48;

support_ai.Widget = class {
	constructor() {
		this.conversation = null;
		this.is_open = false;
		this.is_minimized = false;
		this.is_maximized = false;
		this.is_history_open = false;
		this.reference_doctype = null;
		this.reference_name = null;
		this.normal_rect = null; // {left, top, width, height} saved across maximize/minimize
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
					<button class="support-ai-icon-btn support-ai-history-btn" title="${__("Chat history")}">${ICONS.history}</button>
					<button class="support-ai-icon-btn support-ai-new-btn" title="${__("New Conversation")}">${ICONS.plus}</button>
					<button class="support-ai-icon-btn support-ai-minimize-btn" title="${__("Minimize")}">${ICONS.minimize}</button>
					<button class="support-ai-icon-btn support-ai-maximize-btn" title="${__("Maximize")}">${ICONS.maximize}</button>
					<button class="support-ai-icon-btn support-ai-close-btn" title="${__("Close")}">${ICONS.close}</button>
				</div>
				<div class="support-ai-history-panel">
					<div class="support-ai-history-toolbar">
						<button class="support-ai-icon-btn support-ai-history-back-btn" title="${__("Back to chat")}">${ICONS.back}</button>
						<div class="support-ai-history-search-box">
							${ICONS.search}
							<input type="text" placeholder="${__("Search your chat history...")}" />
						</div>
					</div>
					<div class="support-ai-history-list"></div>
				</div>
				<div class="support-ai-body">
					<div class="support-ai-messages"></div>
					<div class="support-ai-input-row">
						<textarea rows="2" placeholder="${__("Describe the issue, e.g. 'this invoice won't submit, what's wrong?'")}"></textarea>
						<button class="btn btn-primary btn-sm support-ai-send-btn">${__("Ask")}</button>
					</div>
				</div>
			</div>
		`).appendTo("body");

		this.$header = this.$panel.find(".support-ai-panel-header");
		this.$body = this.$panel.find(".support-ai-body");
		this.$messages = this.$panel.find(".support-ai-messages");
		this.$context = this.$panel.find(".support-ai-context");
		this.$input = this.$panel.find(".support-ai-input-row textarea");
		this.$historyPanel = this.$panel.find(".support-ai-history-panel");
		this.$historyInput = this.$historyPanel.find("input");
		this.$historyList = this.$panel.find(".support-ai-history-list");
		this.$minimizeBtn = this.$panel.find(".support-ai-minimize-btn");
		this.$maximizeBtn = this.$panel.find(".support-ai-maximize-btn");

		this.$bubble.on("click", () => this.toggle());
		this.$panel.find(".support-ai-close-btn").on("click", () => this.toggle(false));
		this.$panel.find(".support-ai-new-btn").on("click", () => this.start_new_conversation());
		this.$panel.find(".support-ai-send-btn").on("click", () => this.send());
		this.$minimizeBtn.on("click", (e) => {
			e.stopPropagation();
			this.set_minimized(!this.is_minimized);
		});
		this.$maximizeBtn.on("click", (e) => {
			e.stopPropagation();
			this.set_maximized(!this.is_maximized);
		});
		this.$panel.find(".support-ai-history-btn").on("click", () => this.toggle_history(true));
		this.$panel.find(".support-ai-history-back-btn").on("click", () => this.toggle_history(false));
		this.$historyInput.on(
			"input",
			frappe.utils.debounce(() => this.run_search(), 300)
		);

		this.$input.on("keydown", (e) => {
			if (e.key === "Enter" && !e.shiftKey) {
				e.preventDefault();
				this.send();
			}
		});

		// Always-works fallback: closes the panel no matter what state the
		// header buttons are in (a stale cached JS bundle, or any future
		// rendering bug, shouldn't be able to trap someone with no way out).
		$(document).on("keydown.support_ai_esc", (e) => {
			if (e.key === "Escape" && this.is_open) this.toggle(false);
		});

		this.setup_drag();
	}

	// --- Window chrome: drag / minimize / maximize -------------------------

	setup_drag() {
		let dragging = false;
		let startX, startY, startLeft, startTop;

		this.$header.on("mousedown", (e) => {
			if ($(e.target).closest("button").length) return;
			if (this.is_maximized) return;

			dragging = true;
			const rect = this.$panel[0].getBoundingClientRect();
			startX = e.clientX;
			startY = e.clientY;
			startLeft = rect.left;
			startTop = rect.top;
			this.$panel.css({ right: "auto", bottom: "auto", left: startLeft + "px", top: startTop + "px" });
			e.preventDefault();
		});

		$(document).on("mousemove.support_ai_drag", (e) => {
			if (!dragging) return;
			const w = this.$panel.outerWidth();
			const h = this.$panel.outerHeight();
			let newLeft = startLeft + (e.clientX - startX);
			let newTop = startTop + (e.clientY - startY);
			newLeft = Math.max(0, Math.min(newLeft, window.innerWidth - w));
			newTop = Math.max(0, Math.min(newTop, window.innerHeight - h));
			this.$panel.css({ left: newLeft + "px", top: newTop + "px" });
		});

		$(document).on("mouseup.support_ai_drag", () => {
			dragging = false;
		});
	}

	current_rect() {
		const rect = this.$panel[0].getBoundingClientRect();
		return { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
	}

	apply_rect(rect) {
		this.$panel.css({
			right: "auto",
			bottom: "auto",
			left: rect.left + "px",
			top: rect.top + "px",
			width: rect.width + "px",
			height: rect.height + "px",
		});
	}

	set_minimized(minimized) {
		if (this.is_maximized) this.set_maximized(false);
		this.is_minimized = minimized;

		if (minimized) {
			this.normal_rect = this.current_rect();
			this.$panel.css("height", MINIMIZED_HEIGHT + "px");
			this.$body.hide();
			this.$historyPanel.hide();
			this.$minimizeBtn.html(ICONS.restore).attr("title", __("Restore"));
		} else {
			if (this.is_history_open) this.$historyPanel.show();
			else this.$body.show();
			this.$minimizeBtn.html(ICONS.minimize).attr("title", __("Minimize"));
			if (this.normal_rect) this.$panel.css("height", this.normal_rect.height + "px");
		}
	}

	set_maximized(maximized) {
		if (this.is_minimized) this.set_minimized(false);
		this.is_maximized = maximized;

		if (maximized) {
			this.normal_rect = this.current_rect();
			this.apply_rect({
				left: window.innerWidth * 0.05,
				top: window.innerHeight * 0.05,
				width: window.innerWidth * 0.9,
				height: window.innerHeight * 0.9,
			});
			this.$maximizeBtn.html(ICONS.restore).attr("title", __("Restore"));
		} else {
			this.$maximizeBtn.html(ICONS.maximize).attr("title", __("Maximize"));
			if (this.normal_rect) this.apply_rect(this.normal_rect);
		}
	}

	toggle(force) {
		this.is_open = force !== undefined ? force : !this.is_open;
		this.$panel.toggleClass("support-ai-panel-open", this.is_open);
		if (this.is_open) {
			if (!this.normal_rect) {
				// First open: dock bottom-right at the default size.
				this.$panel.css({
					right: "24px",
					bottom: "90px",
					left: "auto",
					top: "auto",
					width: DEFAULT_WIDTH + "px",
					height: DEFAULT_HEIGHT + "px",
				});
			}
			this.sync_with_current_document();
			this.$input.trigger("focus");
		}
	}

	// --- History / search --------------------------------------------------

	toggle_history(show) {
		this.is_history_open = show;
		this.$historyPanel.toggle(show);
		this.$body.toggle(!show);
		if (show) {
			this.$historyInput.val("");
			this.run_search();
			this.$historyInput.trigger("focus");
		}
	}

	run_search() {
		const query = (this.$historyInput.val() || "").trim();
		frappe.call({
			method: "support_ai.api.search_conversations",
			args: { query },
			callback: (r) => {
				this.$historyList.empty();
				const results = r.message || [];
				if (!results.length) {
					this.$historyList.append(`<div class="support-ai-history-empty">${__("No conversations found")}</div>`);
					return;
				}
				results.forEach((c) => {
					const $item = $(`
						<div class="support-ai-history-item">
							<div class="support-ai-history-item-title">${frappe.utils.escape_html(c.title || c.name)}</div>
							<div class="support-ai-history-item-meta">${frappe.datetime.comment_when(c.modified)}</div>
						</div>
					`);
					$item.on("click", () => {
						this.load_conversation(c.name);
						this.toggle_history(false);
					});
					this.$historyList.append($item);
				});
			},
		});
	}

	// --- Conversation lifecycle -----------------------------------------------

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
				this.render_message("Assistant", greeting, { editable: false });
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
				this.update_context_label();
				this.$messages.empty();
				(convo.messages || []).forEach((m) => this.render_message(m.role, m.content));
			},
		});
	}

	// --- Sending / editing messages --------------------------------------------

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
		const $pending = this.render_message("Assistant", __("Thinking... (0s)"), { editable: false, pending: true });
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
				this.render_message("Assistant", r.message.reply, { editable: false });
			},
			error: () => {
				clearInterval(timer);
				$pending.remove();
				this.render_message("Assistant", __("Something went wrong reaching Support AI. Please try again."), { editable: false });
			},
		});
	}

	edit_message($row) {
		const idx = this.$messages.children().index($row);
		const original_text = $row.data("raw-content");

		frappe.call({
			method: "support_ai.api.edit_message",
			args: { conversation: this.conversation, keep_count: idx },
			callback: () => {
				// Drop this message and everything after it from the view too.
				this.$messages.children().slice(idx).remove();
				this.$input.val(original_text).trigger("focus");
			},
		});
	}

	render_message(role, content, opts) {
		opts = opts || {};
		const is_user = role === "User";
		const editable = opts.editable !== false && is_user;

		const $row = $(`
			<div class="support-ai-msg-row ${is_user ? "support-ai-msg-user" : "support-ai-msg-assistant"}">
				${editable ? `<button class="support-ai-edit-btn" title="${__("Edit and resend")}">${ICONS.edit}</button>` : ""}
				<div class="support-ai-msg-bubble"></div>
			</div>
		`);
		$row.data("raw-content", content);
		$row.find(".support-ai-msg-bubble").text(content);
		if (editable) {
			$row.find(".support-ai-edit-btn").on("click", (e) => {
				e.stopPropagation();
				this.edit_message($row);
			});
		}
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
