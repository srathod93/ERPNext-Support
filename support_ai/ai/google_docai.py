# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Google Document AI: OCR / form parsing of attached PDFs and images, and
management of the Document AI processors used for it."""

import json

import frappe
from frappe import _

MIME_TYPES = {
	"jpg": "image/jpeg",
	"jpeg": "image/jpeg",
	"png": "image/png",
	"pdf": "application/pdf",
	"tif": "image/tiff",
	"tiff": "image/tiff",
	"webp": "image/webp",
	"gif": "image/gif",
}

PROCESSOR_TYPES = {
	"OCR_PROCESSOR": "Enterprise Document OCR - text from any document, incl. handwriting",
	"FORM_PARSER_PROCESSOR": "Form Parser - key/value pairs, checkboxes and tables",
	"INVOICE_PROCESSOR": "Invoice Parser - invoice number, supplier, amounts, taxes, line items",
	"EXPENSE_PROCESSOR": "Receipt Parser - merchant, date, total, tax, line items",
	"BANK_STATEMENT_PROCESSOR": "Bank Statement Parser - account info, transactions, balances",
}


def _settings():
	settings = frappe.get_cached_doc("Support AI Settings")
	if not settings.get("enable_google_document_ai"):
		frappe.throw(_("Google Document AI is not enabled in Support AI Settings."))
	if not settings.get("google_project_id"):
		frappe.throw(_("Google Project ID is not set in Support AI Settings."))
	return settings


def _client(settings):
	from google.api_core.client_options import ClientOptions
	from google.cloud import documentai
	from google.oauth2 import service_account

	key = settings.get_password("google_service_account_json_key", raise_exception=False)
	if not key:
		frappe.throw(_("Google Service Account JSON Key is not set in Support AI Settings."))

	credentials = service_account.Credentials.from_service_account_info(json.loads(key))
	location = settings.get("google_processor_location") or "us"
	options = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
	return documentai.DocumentProcessorServiceClient(credentials=credentials, client_options=options)


def is_supported(extension):
	return (extension or "").lower() in MIME_TYPES


def process_file(file_doc, processor_id=None):
	"""Returns the parsed text of a File: form fields as 'key: value' lines
	when the processor finds any, otherwise the full OCR text."""
	from google.cloud import documentai

	settings = _settings()
	processor_id = processor_id or settings.get("google_default_processor_id")
	if not processor_id:
		frappe.throw(_("No Document AI processor is set on the bot or in Support AI Settings."))

	extension = (file_doc.file_name or file_doc.file_url).rsplit(".", 1)[-1].lower()
	if extension not in MIME_TYPES:
		return ""

	client = _client(settings)
	name = client.processor_path(settings.get("google_project_id"), settings.get("google_processor_location") or "us", processor_id)
	raw_document = documentai.RawDocument(content=file_doc.get_content(), mime_type=MIME_TYPES[extension])
	result = client.process_document(request=documentai.ProcessRequest(name=name, raw_document=raw_document))
	document = result.document

	form_fields = {}
	for page in document.pages or []:
		for form_field in page.form_fields or []:
			key = (form_field.field_name.text_anchor.content or "").strip()
			value = (form_field.field_value.text_anchor.content or "").strip()
			if key:
				form_fields[key] = "Yes" if value == "☑" else value

	entities = [
		f"{e.type_}: {(e.mention_text or '').strip()}" for e in (document.entities or []) if e.mention_text
	]

	parts = []
	if form_fields:
		parts.append("\n".join(f"{k}: {v}" for k, v in form_fields.items()))
	if entities:
		parts.append("\n".join(entities))
	if not parts:
		parts.append(document.text or "")
	return "\n\n".join(parts)


@frappe.whitelist()
def get_processor_types():
	frappe.only_for("System Manager")
	return PROCESSOR_TYPES


@frappe.whitelist()
def list_processors():
	frappe.only_for("System Manager")
	settings = _settings()
	client = _client(settings)
	parent = client.common_location_path(settings.get("google_project_id"), settings.get("google_processor_location") or "us")
	return [
		{
			"id": p.name.split("/")[-1],
			"display_name": p.display_name,
			"type": p.type_,
			"state": str(p.state).rsplit(".", 1)[-1],
		}
		for p in client.list_processors(parent=parent)
	]


@frappe.whitelist(methods=["POST"])
def create_processor(processor_type):
	"""Creating a processor is billable on Google Cloud - System Manager only."""
	from google.cloud import documentai

	frappe.only_for("System Manager")
	if processor_type not in PROCESSOR_TYPES:
		frappe.throw(_("Invalid processor type: {0}").format(processor_type))

	settings = _settings()
	client = _client(settings)
	parent = client.common_location_path(settings.get("google_project_id"), settings.get("google_processor_location") or "us")
	processor = client.create_processor(
		parent=parent,
		processor=documentai.Processor(display_name=f"SupportAI-{processor_type}", type_=processor_type),
	)
	return {"id": processor.name.split("/")[-1], "display_name": processor.display_name, "type": processor.type_}


@frappe.whitelist(methods=["POST"])
def delete_processor(processor_id):
	frappe.only_for("System Manager")
	settings = _settings()
	client = _client(settings)
	client.delete_processor(
		name=client.processor_path(settings.get("google_project_id"), settings.get("google_processor_location") or "us", processor_id)
	)

	for bot in frappe.get_all("Support AI Bot", filters={"google_processor_id": processor_id}, pluck="name"):
		frappe.db.set_value("Support AI Bot", bot, {"use_google_document_ai": 0, "google_processor_id": None})
	if settings.google_default_processor_id == processor_id:
		frappe.db.set_single_value("Support AI Settings", "google_default_processor_id", None)

	return {"message": _("Processor deleted")}
