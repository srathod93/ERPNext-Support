// Adds "Ask Support AI" to the "..." menu of every comment and email in a
// form's timeline, so a user can start an AI thread from that exact message
// (the thread gets the message text plus the document's diagnostic context).
frappe.provide("support_ai");

support_ai.patch_timeline = function (proto) {
	if (proto.__support_ai_patched) return false;
	proto.__support_ai_patched = true;

	const add_item = (wrapper, source_doctype, source_name) => {
		const $menu = wrapper.find(".more-actions > .dropdown-menu > li").first();
		if (!$menu.length || !source_name) return;
		$(`<a class="dropdown-item">${__("Ask Support AI")}</a>`)
			.on("click", () => {
				if (support_ai.widget) support_ai.widget.start_thread_from(source_doctype, source_name);
			})
			.appendTo($menu);
	};

	const original_comment_actions = proto.setup_comment_actions;
	proto.setup_comment_actions = function (comment_wrapper, doc) {
		original_comment_actions.call(this, comment_wrapper, doc);
		add_item(comment_wrapper, "Comment", doc && doc.name);
	};

	const original_setup_reply = proto.setup_reply;
	proto.setup_reply = function (communication_box, communication_doc) {
		original_setup_reply.call(this, communication_box, communication_doc);
		add_item(communication_box, "Communication", communication_doc && communication_doc.name);
	};
	return true;
};

frappe.ui.form.on("*", {
	refresh(frm) {
		// FormTimeline isn't exported globally, so patch its prototype through
		// the first form instance; every later timeline render uses the patch.
		if (!frm.timeline || !frm.timeline.setup_comment_actions) return;
		if (support_ai.patch_timeline(Object.getPrototypeOf(frm.timeline))) {
			frm.timeline.refresh();
		}
	},
});
