app_name = "support_ai"
app_title = "Support AI"
app_publisher = "shyam8848"
app_description = "AI-powered self-hosted diagnostic support assistant for Frappe/ERPNext"
app_email = "shyam@8848digital.com"
app_license = "mit"

custom_fixtures = [
	{
		"dt": "Custom Field",
		"filters": {"module": ["in", ["Support AI"]]},
	}
]

commands = ["support_ai.commands.export_fixtures.export_fixtures"]

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "support_ai",
# 		"logo": "/assets/support_ai/logo.png",
# 		"title": "Support AI",
# 		"route": "/support_ai",
# 		"has_permission": "support_ai.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
#
# NOTE: these are plain /assets/... paths, not a bundled .bundle.js entry, so
# Frappe's normal cache-busting (a content hash from assets.json) never
# applies to them - the server also sends Cache-Control: max-age=43200 (12h)
# on static files. Without a versioned query string, a browser can keep
# serving a stale cached copy for up to 12 hours even across normal reloads.
# ASSET_VERSION below is the cache-buster: bump it on every edit to either
# file below, or changes won't reliably reach already-open browser tabs.
ASSET_VERSION = "10"
app_include_js = [
	f"/assets/support_ai/js/support_ai_message_renderer.js?v={ASSET_VERSION}",
	f"/assets/support_ai/js/support_ai_actions.js?v={ASSET_VERSION}",
	f"/assets/support_ai/js/support_ai_widget.js?v={ASSET_VERSION}",
	f"/assets/support_ai/js/support_ai_error_help.js?v={ASSET_VERSION}",
	f"/assets/support_ai/js/support_ai_form_button.js?v={ASSET_VERSION}",
	f"/assets/support_ai/js/support_ai_timeline.js?v={ASSET_VERSION}",
]

after_install = "support_ai.install.after_install"
after_migrate = "support_ai.install.after_migrate"

scheduler_events = {
	"daily": ["support_ai.support_ai.tasks.send_daily_digest"],
}
app_include_css = f"/assets/support_ai/css/support_ai_widget.css?v={ASSET_VERSION}"

# include js, css files in header of web template
# web_include_css = "/assets/support_ai/css/support_ai.css"
# web_include_js = "/assets/support_ai/js/support_ai.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "support_ai/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "support_ai/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "support_ai.utils.jinja_methods",
# 	"filters": "support_ai.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "support_ai.install.before_install"
# after_install = "support_ai.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "support_ai.uninstall.before_uninstall"
# after_uninstall = "support_ai.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "support_ai.utils.before_app_install"
# after_app_install = "support_ai.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "support_ai.utils.before_app_uninstall"
# after_app_uninstall = "support_ai.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "support_ai.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "support_ai.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"support_ai.tasks.all"
# 	],
# 	"daily": [
# 		"support_ai.tasks.daily"
# 	],
# 	"hourly": [
# 		"support_ai.tasks.hourly"
# 	],
# 	"weekly": [
# 		"support_ai.tasks.weekly"
# 	],
# 	"monthly": [
# 		"support_ai.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "support_ai.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "support_ai.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "support_ai.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "support_ai.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["support_ai.utils.before_request"]
# after_request = ["support_ai.utils.after_request"]

# Job Events
# ----------
# before_job = ["support_ai.utils.before_job"]
# after_job = ["support_ai.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"support_ai.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

