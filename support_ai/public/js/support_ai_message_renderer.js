// Raven-style message rendering for the Support AI widget: flat rows with an
// avatar + "Name | time" header, hover action toolbar, date separators, a
// channel-style intro and a typing indicator.
frappe.provide("support_ai");

const RENDER_ICONS = (support_ai.render_icons = {
	bot: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/></svg>`,
	copy: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>`,
	bookmark: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M6 3h12v18l-6-4-6 4z"/></svg>`,
	file: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>`,
	info: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16"/><line x1="12" y1="8" x2="12" y2="8"/></svg>`,
});

support_ai.MessageRenderer = class {
	/** @param {object} widget - the owning support_ai.Widget (for action callbacks) */
	constructor(widget) {
		this.widget = widget;
		this.last_date = null;
	}

	reset() {
		this.last_date = null;
	}

	// --- Pieces -------------------------------------------------------------------

	avatar(role, user) {
		if (role === "User") {
			return `<div class="support-ai-avatar">${frappe.avatar(user || frappe.session.user, "avatar-medium")}</div>`;
		}
		return `<div class="support-ai-avatar"><span class="support-ai-bot-avatar">${RENDER_ICONS.bot}</span></div>`;
	}

	sender_name(role, user) {
		if (role === "User") return frappe.user.full_name(user || frappe.session.user);
		return this.widget.bot || __("Support AI");
	}

	time_label(sent_at) {
		return moment(sent_at || frappe.datetime.now_datetime()).format("h:mm A");
	}

	date_separator(sent_at) {
		const date = moment(sent_at || frappe.datetime.now_datetime());
		const key = date.format("YYYY-MM-DD");
		if (key === this.last_date) return null;
		this.last_date = key;

		const today = moment().startOf("day");
		let label = date.format("D MMMM YYYY");
		if (date.isSame(today, "day")) label = __("Today");
		else if (date.isSame(today.clone().subtract(1, "day"), "day")) label = __("Yesterday");
		return $(`<div class="support-ai-date-separator"><span></span></div>`).find("span").text(label).end();
	}

	/** Escapes everything first, then applies a small safe markdown subset
	 * (code blocks, inline code, bold, headings, lists, http links). */
	markdown(text) {
		// Not frappe.utils.escape_html - it also escapes ` and =, which would
		// hide code fences and break link URLs.
		const escaped = String(text || "").replace(
			/[&<>"']/g,
			(char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]
		);
		return escaped
			.split(/```/)
			.map((part, i) => (i % 2 ? this.code_block(part) : this.markdown_blocks(part)))
			.join("");
	}

	code_block(part) {
		const body = part.replace(/^[\w+-]*\n/, "");
		return `<pre class="support-ai-code"><code>${body.replace(/\n$/, "")}</code></pre>`;
	}

	markdown_blocks(part) {
		const html = [];
		let list = null;
		let table = [];
		const close_list = () => {
			if (list) html.push(`</${list}>`);
			list = null;
		};
		const close_table = () => {
			if (table.length) html.push(this.table(table));
			table = [];
		};
		part.split("\n").forEach((line) => {
			if (/^\s*\|.*\|\s*$/.test(line)) {
				close_list();
				table.push(line.trim());
				return;
			}
			close_table();
			const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
			const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
			const heading = line.match(/^\s*#{1,4}\s+(.*)$/);
			if (bullet || numbered) {
				const type = bullet ? "ul" : "ol";
				if (list !== type) {
					close_list();
					html.push(`<${type}>`);
					list = type;
				}
				html.push(`<li>${this.inline((bullet || numbered)[1])}</li>`);
				return;
			}
			close_list();
			if (heading) html.push(`<div class="support-ai-md-heading">${this.inline(heading[1])}</div>`);
			else if (line.trim()) html.push(`<p>${this.inline(line)}</p>`);
		});
		close_list();
		close_table();
		return html.join("");
	}

	/**
	 * Renders pipe-table lines ("| a | b |") as an HTML table; the second
	 * line ("|---|---|") marks the header row.
	 *
	 * @param {string[]} lines - Escaped table lines.
	 * @returns {string} Table HTML in a horizontally scrollable wrapper.
	 */
	table(lines) {
		const cells = (line) => line.replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
		const has_header = lines.length > 1 && /^\|?\s*:?-{2,}/.test(lines[1]);
		const head = has_header ? `<thead><tr>${cells(lines[0]).map((c) => `<th>${this.inline(c)}</th>`).join("")}</tr></thead>` : "";
		const body_lines = has_header ? lines.slice(2) : lines;
		const body = body_lines.map((l) => `<tr>${cells(l).map((c) => `<td>${this.inline(c)}</td>`).join("")}</tr>`).join("");
		return `<div class="support-ai-table-wrap"><table class="support-ai-table">${head}<tbody>${body}</tbody></table></div>`;
	}

	/**
	 * Inline markdown: code, bold, _italic_, external links and in-app
	 * /app/... links (opened with the Desk router, not a page reload).
	 *
	 * @param {string} text - Escaped text.
	 * @returns {string} HTML.
	 */
	inline(text) {
		return text
			.replace(/`([^`]+)`/g, "<code>$1</code>")
			.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
			.replace(/(^|\s)_([^_]+)_(?=\s|$)/g, "$1<em>$2</em>")
			.replace(/\[([^\]]+)\]\((\/app\/[^\s)]+)\)/g, '<a href="$2" class="support-ai-route-link">$1</a>')
			.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
	}

	// --- Blocks -------------------------------------------------------------------

	intro({ title, description, context, suggestions }) {
		const $intro = $(`
			<div class="support-ai-intro">
				<div class="support-ai-intro-title">
					<span class="support-ai-bot-avatar support-ai-bot-avatar-lg">${RENDER_ICONS.bot}</span>
					<span class="support-ai-intro-name"></span>
				</div>
				<div class="support-ai-intro-text"></div>
				<div class="support-ai-intro-actions">
					<a class="support-ai-intro-attach">${RENDER_ICONS.file} ${__("Attach a file")}</a>
					<a class="support-ai-intro-prompts">${RENDER_ICONS.bookmark} ${__("Saved prompts")}</a>
				</div>
			</div>
		`);
		$intro.find(".support-ai-intro-name").text(title);
		const $text = $intro.find(".support-ai-intro-text").text(description);
		if (context) $text.append(" ", $("<strong>").text(context));
		$intro.find(".support-ai-intro-attach").on("click", () => this.widget.attach_file());
		$intro.find(".support-ai-intro-prompts").on("click", () => this.widget.show_prompts());
		if (suggestions && suggestions.length) {
			$intro.append(this.widget.actions.chips(suggestions.map((s) => ({ label: s, prompt: s }))));
		}
		return $intro;
	}

	system_note(content) {
		const $note = $(`<div class="support-ai-system-note"><span class="support-ai-system-icon">${RENDER_ICONS.info}</span><div></div></div>`);
		$note.find("div").text(content);
		return $note;
	}

	typing(label) {
		const $row = $(`
			<div class="support-ai-row support-ai-typing">
				${this.avatar("Assistant")}
				<div class="support-ai-row-main">
					<div class="support-ai-row-header"><span class="support-ai-sender"></span></div>
					<div class="support-ai-stream-text support-ai-text"></div>
					<div class="support-ai-typing-text"><span class="support-ai-dots"><i></i><i></i><i></i></span><span class="support-ai-typing-label"></span></div>
				</div>
			</div>
		`);
		$row.find(".support-ai-sender").text(this.sender_name("Assistant"));
		$row.find(".support-ai-typing-label").text(label);
		return $row;
	}

	/** opts: index, sent_at, user, thinking, debug_info, attachments, continuation, actions, feedback, answered_by */
	message(role, content, opts) {
		const is_user = role === "User";
		const stored = opts.index !== undefined && opts.index !== null;

		const $row = $(`
			<div class="support-ai-row ${is_user ? "support-ai-row-user" : "support-ai-row-assistant"}">
				${opts.continuation ? `<div class="support-ai-avatar support-ai-avatar-gap"><span class="support-ai-hover-time"></span></div>` : this.avatar(role, opts.user)}
				<div class="support-ai-row-main">
					${opts.continuation ? "" : `<div class="support-ai-row-header"><span class="support-ai-sender"></span>${is_user ? "" : `<span class="support-ai-badge">${__("Bot")}</span>`}<span class="support-ai-header-divider"></span><span class="support-ai-time"></span></div>`}
					<div class="support-ai-row-body"></div>
				</div>
				<div class="support-ai-row-actions"></div>
			</div>
		`);
		$row.find(".support-ai-sender").text(this.sender_name(role, opts.user));
		$row.find(".support-ai-time, .support-ai-hover-time").text(this.time_label(opts.sent_at));
		$row.data({ "raw-content": content, index: opts.index });

		const $body = $row.find(".support-ai-row-body");
		if (opts.thinking) $body.append(this.details(__("Show reasoning"), opts.thinking, "div"));
		$body.append(
			is_user
				? $(`<div class="support-ai-text support-ai-text-plain"></div>`).text(content)
				: $(`<div class="support-ai-text"></div>`).html(this.markdown(content))
		);
		this.append_attachments($body, opts.attachments);
		if (!is_user) $body.append(this.widget.actions.render(opts));
		if (opts.debug_info) $body.append(this.details(__("Debug"), opts.debug_info, "pre"));

		this.add_actions($row, { is_user, stored, content });
		return $row;
	}

	details(summary, text, tag) {
		const $details = $(`<details class="support-ai-details"><summary></summary><${tag}></${tag}></details>`);
		$details.find("summary").text(summary);
		$details.find(tag).text(text);
		return $details;
	}

	append_attachments($body, attachments) {
		if (!attachments || !attachments.length) return;
		const $files = $(`<div class="support-ai-files"></div>`);
		attachments.forEach((a) => {
			const url = typeof a === "string" ? a : a.file_url;
			const name = typeof a === "string" ? a.split("/").pop() : a.file_name;
			const $file = $(`<a class="support-ai-file" target="_blank">${RENDER_ICONS.file}<span></span></a>`).attr("href", url);
			$file.find("span").text(name);
			$files.append($file);
		});
		$body.append($files);
	}

	add_actions($row, { is_user, stored, content }) {
		const $actions = $row.find(".support-ai-row-actions");
		const icons = this.widget.icons;
		const add = (icon, title, handler) =>
			$(`<button class="support-ai-action-btn" title="${title}">${icon}</button>`)
				.on("click", (e) => {
					e.stopPropagation();
					handler();
				})
				.appendTo($actions);

		add(RENDER_ICONS.copy, __("Copy"), () => frappe.utils.copy_to_clipboard(content));
		if (is_user) add(RENDER_ICONS.bookmark, __("Save as prompt"), () => this.widget.save_prompt_from(content));
		if (is_user && stored) add(icons.edit, __("Edit and resend"), () => this.widget.edit_message($row));
		if (stored) add(icons.branch, __("Branch a new conversation from here"), () => this.widget.branch_from($row.data("index")));
	}
};
