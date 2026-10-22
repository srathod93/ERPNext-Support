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
	branch: `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10"/><path d="M18 10c0 4-6 3-11.5 7"/></svg>`,
	search: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65" stroke-linecap="round"/></svg>`,
	back: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>`,
	attach: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5l-8.6 8.6a5 5 0 0 1-7.1-7.1l8.6-8.6a3.3 3.3 0 0 1 4.7 4.7l-8.6 8.6a1.7 1.7 0 0 1-2.4-2.4l7.9-7.9"/></svg>`,
	send: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12l16-8-6 16-2.5-6.5z"/><path d="M11.5 13.5L20 4"/></svg>`,
	prompts: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M6 3h12v18l-6-4-6 4z"/></svg>`,
};

const DEFAULT_WIDTH = 420;
const DEFAULT_HEIGHT = 600;
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
		this.bots = [];
		this.bot = null;
		this.pending_attachments = []; // [{file_url, file_name}]
		// Index the next stored message will get on the server - the greeting
		// shown in a new chat isn't stored, so DOM position can't be used.
		this.stored_count = 0;
		this.icons = ICONS;
		this.renderer = new support_ai.MessageRenderer(this);
		this.last_row = null; // {role, sent_at} of the last rendered message, for grouping
		this.make();
		this.load_bots();
	}

	make() {
		this.$bubble = $(`<div class="support-ai-bubble" title="${__("Ask Support AI")}">${SUPPORT_AI_BUBBLE_SVG}</div>`).appendTo(
			"body"
		);

		this.$panel = $(`
			<div class="support-ai-panel">
				<div class="support-ai-panel-header">
					<div class="support-ai-header-identity">
						<span class="support-ai-bot-avatar support-ai-bot-avatar-sm">${support_ai.render_icons.bot}</span>
						<div class="support-ai-header-text">
							<div class="support-ai-title">${__("Support AI")}</div>
							<div class="support-ai-context"></div>
						</div>
					</div>
					<button class="support-ai-icon-btn support-ai-history-btn" title="${__("Chat history")}">${ICONS.history}</button>
					<button class="support-ai-icon-btn support-ai-new-btn" title="${__("New Conversation")}">${ICONS.plus}</button>
					<button class="support-ai-icon-btn support-ai-minimize-btn" title="${__("Minimize")}">${ICONS.minimize}</button>
					<button class="support-ai-icon-btn support-ai-maximize-btn" title="${__("Maximize")}">${ICONS.maximize}</button>
					<button class="support-ai-icon-btn support-ai-close-btn" title="${__("Close")}">${ICONS.close}</button>
				</div>
				<div class="support-ai-bot-row" style="display:none">
					<select class="form-control input-xs support-ai-bot-select"></select>
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
					<div class="support-ai-composer-wrap">
						<div class="support-ai-composer">
							<div class="support-ai-pending-attachments"></div>
							<textarea rows="1" placeholder="${__("Describe the issue, e.g. 'this invoice won't submit, what's wrong?'")}"></textarea>
							<div class="support-ai-composer-toolbar">
								<button class="support-ai-icon-btn support-ai-attach-btn" title="${__("Attach file")}">${ICONS.plus}</button>
								<span class="support-ai-toolbar-divider"></span>
								<button class="support-ai-icon-btn support-ai-prompts-btn" title="${__("Saved prompts")}">${ICONS.prompts}</button>
								<span class="support-ai-composer-hint">${__("Enter to send · Shift + Enter for a new line")}</span>
								<button class="support-ai-send-btn" title="${__("Send")}" disabled>${ICONS.send}</button>
							</div>
						</div>
					</div>
				</div>
			</div>
		`).appendTo("body");

		this.$header = this.$panel.find(".support-ai-panel-header");
		this.$body = this.$panel.find(".support-ai-body");
		this.$messages = this.$panel.find(".support-ai-messages");
		this.$context = this.$panel.find(".support-ai-context");
		this.$input = this.$panel.find(".support-ai-composer textarea");
		this.$send = this.$panel.find(".support-ai-send-btn");
		this.$title = this.$panel.find(".support-ai-title");
		this.$historyPanel = this.$panel.find(".support-ai-history-panel");
		this.$historyInput = this.$historyPanel.find("input");
		this.$historyList = this.$panel.find(".support-ai-history-list");
		this.$minimizeBtn = this.$panel.find(".support-ai-minimize-btn");
		this.$maximizeBtn = this.$panel.find(".support-ai-maximize-btn");
		this.$botRow = this.$panel.find(".support-ai-bot-row");
		this.$botSelect = this.$panel.find(".support-ai-bot-select");
		this.$pending = this.$panel.find(".support-ai-pending-attachments");

		this.$bubble.on("click", () => this.toggle());
		this.$panel.find(".support-ai-close-btn").on("click", () => this.toggle(false));
		this.$panel.find(".support-ai-new-btn").on("click", () => this.start_new_conversation());
		this.$panel.find(".support-ai-send-btn").on("click", () => this.send());
		this.$panel.find(".support-ai-attach-btn").on("click", () => this.attach_file());
		this.$panel.find(".support-ai-prompts-btn").on("click", () => this.show_prompts());
		this.$botSelect.on("change", () => {
			this.set_bot(this.$botSelect.val());
			this.start_new_conversation();
		});
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

		this.$input.on("input", () => this.refresh_composer());
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

	// --- Bots ----------------------------------------------------------------------

	load_bots() {
		frappe.call({
			method: "support_ai.api.get_available_bots",
			callback: (r) => {
				const res = r.message || {};
				this.bots = res.bots || [];
				this.bot = res.default || null;
				this.$botSelect.empty();
				this.$botSelect.append(`<option value="">${__("Support AI (default)")}</option>`);
				this.bots.forEach((b) => {
					$("<option>").val(b.name).text(b.name).attr("title", b.description || "").appendTo(this.$botSelect);
				});
				this.set_bot(this.bot);
				this.$botRow.toggle(this.bots.length > 0);
			},
		});
	}

	set_bot(bot) {
		this.bot = bot || null;
		this.$botSelect.val(this.bot || "");
		this.$title.text(this.bot || __("Support AI"));
	}

	// Auto-grows the composer (up to the CSS max-height) and enables Send
	// only when there is something to send.
	refresh_composer() {
		const el = this.$input[0];
		el.style.height = "auto";
		el.style.height = el.scrollHeight + "px";
		this.$send.prop("disabled", !(this.$input.val() || "").trim());
	}

	set_input(text) {
		this.$input.val(text || "");
		this.refresh_composer();
		this.$input.trigger("focus");
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
			this.$botRow.hide();
			this.$minimizeBtn.html(ICONS.restore).attr("title", __("Restore"));
		} else {
			if (this.is_history_open) this.$historyPanel.show();
			else this.$body.show();
			this.$botRow.toggle(this.bots.length > 0);
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

	open_panel() {
		this.is_open = true;
		this.$panel.addClass("support-ai-panel-open");
		this.$bubble.hide();
		if (!this.normal_rect) {
			// First open: dock bottom-right at the default size, flush
			// with the same margin the bubble itself uses (the bubble
			// is hidden while open, so there's no need to leave extra
			// room below the panel for it).
			this.$panel.css({
				right: "24px",
				bottom: "24px",
				left: "auto",
				top: "auto",
				width: DEFAULT_WIDTH + "px",
				height: DEFAULT_HEIGHT + "px",
			});
		}
		if (this.is_minimized) this.set_minimized(false);
	}

	toggle(force) {
		const open = force !== undefined ? force : !this.is_open;
		if (!open) {
			this.is_open = false;
			this.$panel.removeClass("support-ai-panel-open");
			this.$bubble.show();
			return;
		}
		this.open_panel();
		this.sync_with_current_document();
		this.$input.trigger("focus");
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
							<div class="support-ai-history-item-title"></div>
							<div class="support-ai-history-item-meta"></div>
						</div>
					`);
					$item.find(".support-ai-history-item-title").text(c.title || c.name);
					$item
						.find(".support-ai-history-item-meta")
						.text([c.bot, frappe.datetime.prettyDate(c.modified)].filter(Boolean).join(" · "));
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

	clear_messages() {
		this.$messages.empty();
		this.renderer.reset();
		this.last_row = null;
	}

	show_intro() {
		const has_ref = this.reference_doctype && this.reference_name;
		const bot = this.bots.find((b) => b.name === this.bot);
		const description = has_ref
			? __("Ask why something isn't working, and I'll check its permissions, workflow, validation errors and custom code. Context loaded:")
			: (bot && bot.description) ||
			  __("Describe the issue you're facing, ask for data, or open a document first for a targeted diagnosis.");
		this.$messages.append(
			this.renderer.intro({
				title: this.bot || __("Support AI"),
				description,
				context: has_ref ? `${this.reference_doctype} ${this.reference_name}` : null,
			})
		);
	}

	start_new_conversation() {
		this.clear_messages();
		this.clear_pending_attachments();
		frappe.call({
			method: "support_ai.api.new_conversation",
			args: {
				reference_doctype: this.reference_doctype,
				reference_name: this.reference_name,
				bot: this.bot,
			},
			callback: (r) => {
				this.conversation = r.message;
				this.stored_count = 0;
				this.show_intro();
			},
		});
	}

	// A thread started from a timeline comment / email (Ask Support AI in its
	// "..." menu). The server stores the source text as the first message.
	start_thread_from(source_doctype, source_name) {
		this.open_panel();
		if (this.is_history_open) this.toggle_history(false);
		frappe.call({
			method: "support_ai.api.new_conversation",
			args: { source_doctype, source_name, bot: this.bot },
			callback: (r) => {
				this.load_conversation(r.message, () => {
					this.$input.attr("placeholder", __("What would you like to know about this message?"));
					this.$input.trigger("focus");
				});
			},
		});
	}

	load_conversation(name, after) {
		frappe.call({
			method: "support_ai.api.get_conversation",
			args: { name },
			callback: (r) => {
				const convo = r.message;
				this.conversation = convo.name;
				this.reference_doctype = convo.reference_doctype;
				this.reference_name = convo.reference_name;
				this.set_bot(convo.bot);
				this.update_context_label();
				this.clear_pending_attachments();
				this.clear_messages();
				this.show_intro();
				(convo.messages || []).forEach((m, idx) =>
					this.render_message(m.role, m.content, {
						index: idx,
						sent_at: m.sent_at,
						user: convo.user,
						thinking: m.thinking,
						debug_info: m.debug_info,
						attachments: m.attachments ? JSON.parse(m.attachments) : [],
					})
				);
				this.stored_count = (convo.messages || []).length;
				if (after) after();
			},
		});
	}

	branch_from(index) {
		frappe.call({
			method: "support_ai.api.branch_conversation",
			args: { conversation: this.conversation, message_index: index },
			callback: (r) => {
				this.load_conversation(r.message, () => {
					frappe.show_alert({ message: __("Branched into a new conversation"), indicator: "green" });
					this.$input.trigger("focus");
				});
			},
		});
	}

	// --- Attachments ----------------------------------------------------------------

	attach_file() {
		if (!this.conversation) return;
		new frappe.ui.FileUploader({
			doctype: "Support AI Conversation",
			docname: this.conversation,
			allow_multiple: true,
			make_attachments_public: false,
			on_success: (file_doc) => {
				this.pending_attachments.push({ file_url: file_doc.file_url, file_name: file_doc.file_name });
				this.render_pending_attachments();
			},
		});
	}

	render_pending_attachments() {
		this.$pending.empty();
		this.pending_attachments.forEach((a, i) => {
			const $chip = $(`<span class="support-ai-chip"><span></span><a class="support-ai-chip-x">&times;</a></span>`);
			$chip.find("span").text(a.file_name);
			$chip.find(".support-ai-chip-x").on("click", () => {
				this.pending_attachments.splice(i, 1);
				this.render_pending_attachments();
			});
			this.$pending.append($chip);
		});
		this.$pending.css("display", this.pending_attachments.length ? "flex" : "none");
	}

	clear_pending_attachments() {
		this.pending_attachments = [];
		this.render_pending_attachments();
	}

	// --- Saved prompts --------------------------------------------------------------

	save_prompt_from(text) {
		this.show_prompts(text);
	}

	show_prompts(prefill) {
		frappe.call({
			method: "support_ai.api.get_saved_prompts",
			args: { bot: this.bot },
			callback: (r) => {
				const prompts = r.message || [];
				const d = new frappe.ui.Dialog({
					title: __("Saved Prompts"),
					fields: [
						{ fieldtype: "HTML", fieldname: "list" },
						{ fieldtype: "Section Break", label: __("Save a new prompt") },
						{ fieldtype: "Small Text", fieldname: "prompt", label: __("Prompt"), default: prefill || this.$input.val() },
						{ fieldtype: "Check", fieldname: "for_this_bot", label: __("Only for this bot"), hidden: !this.bot },
						{
							fieldtype: "Check",
							fieldname: "is_global",
							label: __("Share with everyone"),
							hidden: !frappe.user.has_role("System Manager"),
						},
					],
					primary_action_label: __("Save Prompt"),
					primary_action: (values) => {
						if (!values.prompt) return;
						frappe.call({
							method: "support_ai.api.save_prompt",
							args: {
								prompt: values.prompt,
								bot: values.for_this_bot ? this.bot : null,
								is_global: values.is_global ? 1 : 0,
							},
							callback: () => {
								d.hide();
								frappe.show_alert({ message: __("Prompt saved"), indicator: "green" });
							},
						});
					},
				});
				const $list = $(`<div class="support-ai-prompt-list"></div>`);
				if (!prompts.length) {
					$list.append(`<div class="text-muted small">${__("No saved prompts yet.")}</div>`);
				}
				prompts.forEach((p) => {
					const $item = $(`<div class="support-ai-prompt-item"><div class="support-ai-prompt-text"></div><div class="text-muted small"></div></div>`);
					$item.find(".support-ai-prompt-text").text(p.prompt);
					$item.find(".small").text([p.is_global ? __("Shared") : __("Mine"), p.bot].filter(Boolean).join(" · "));
					$item.on("click", () => {
						this.set_input(p.prompt);
						d.hide();
					});
					$list.append($item);
				});
				d.fields_dict.list.$wrapper.append($list);
				d.show();
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

		const attachments = this.pending_attachments.slice();
		this.render_message("User", message, { index: this.stored_count, attachments });
		this.set_input("");
		this.clear_pending_attachments();

		const started_at = Date.now();
		const label = () => __("is thinking... {0}s", [Math.round((Date.now() - started_at) / 1000)]);
		const $pending = this.renderer.typing(label());
		this.append_row($pending);
		const timer = setInterval(() => $pending.find(".support-ai-typing-label").text(label()), 1000);

		frappe.call({
			method: "support_ai.api.ask",
			args: {
				conversation: this.conversation,
				message,
				reference_doctype: this.reference_doctype,
				reference_name: this.reference_name,
				attachments: JSON.stringify(attachments.map((a) => a.file_url)),
			},
			callback: (r) => {
				clearInterval(timer);
				$pending.remove();
				const res = r.message;
				this.stored_count += 2;
				this.render_message("Assistant", res.reply, {
					index: this.stored_count - 1,
					thinking: res.thinking,
					debug_info: res.debug_info,
				});
			},
			error: () => {
				clearInterval(timer);
				$pending.remove();
				// Nothing was stored server-side - reload so the view matches.
				this.load_conversation(this.conversation, () =>
					this.append_row(
						this.renderer.system_note(__("Something went wrong reaching Support AI. Please try again."))
					)
				);
			},
		});
	}

	edit_message($row) {
		const idx = $row.data("index");
		const original_text = $row.data("raw-content");

		frappe.call({
			method: "support_ai.api.edit_message",
			args: { conversation: this.conversation, keep_count: idx },
			callback: () => {
				// Drop this message and everything after it from the view too.
				const pos = this.$messages.children().index($row);
				this.$messages.children().slice(pos).remove();
				this.stored_count = idx;
				this.last_row = null;
				this.set_input(original_text);
			},
		});
	}

	render_message(role, content, opts) {
		opts = opts || {};
		const $separator = this.renderer.date_separator(opts.sent_at);
		if ($separator) {
			this.$messages.append($separator);
			this.last_row = null;
		}

		if (role === "System") {
			this.last_row = null;
			return this.append_row(this.renderer.system_note(content));
		}

		// Like Raven: consecutive messages from the same sender within two
		// minutes are grouped under one avatar/name header.
		const sent_at = moment(opts.sent_at || frappe.datetime.now_datetime());
		opts.continuation = !!(
			this.last_row &&
			this.last_row.role === role &&
			Math.abs(sent_at.diff(this.last_row.sent_at, "minutes")) < 2
		);
		this.last_row = { role, sent_at };
		return this.append_row(this.renderer.message(role, content, opts));
	}

	append_row($row) {
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
