# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Turns user attachments and knowledge-base files into text (or an image
the model can look at). PDFs via pypdf, spreadsheets as markdown tables,
Word files via their document.xml, images/scans via Google Document AI or a
vision-capable model."""

import base64
import csv
import io
import re
import zipfile

import frappe
from frappe import _

TEXT_EXTENSIONS = {"txt", "md", "json", "log", "xml", "html", "htm", "yaml", "yml", "py", "js", "sql", "ini", "conf"}
SPREADSHEET_EXTENSIONS = {"csv", "xlsx"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_SHEET_ROWS = 200
MAX_ATTACHMENTS = 5


def get_extension(file_doc):
	name = file_doc.file_name or file_doc.file_url or ""
	return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def get_permitted_file(file_url):
	file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not file_name:
		frappe.throw(_("File {0} not found.").format(file_url))
	file_doc = frappe.get_doc("File", file_name)
	file_doc.check_permission("read")
	return file_doc


def extract_text(file_doc, use_google=False, processor_id=None):
	"""Best-effort plain text of a file, or "" if it's not a text-bearing type."""
	ext = get_extension(file_doc)

	if use_google:
		from support_ai.ai.google_docai import is_supported, process_file

		if is_supported(ext):
			return process_file(file_doc, processor_id)

	content = file_doc.get_content()
	if isinstance(content, str):
		content = content.encode()

	if ext == "pdf":
		return _pdf_text(content)
	if ext == "csv":
		return _rows_to_markdown(list(csv.reader(io.StringIO(content.decode("utf-8", errors="replace")))))
	if ext == "xlsx":
		return _xlsx_text(content)
	if ext == "docx":
		return _docx_text(content)
	if ext in TEXT_EXTENSIONS:
		return content.decode("utf-8", errors="replace")
	return ""


def _pdf_text(content):
	import pypdf

	reader = pypdf.PdfReader(io.BytesIO(content))
	pages = []
	for i, page in enumerate(reader.pages, 1):
		text = (page.extract_text() or "").strip()
		if text:
			pages.append(f"--- Page {i} ---\n{text}")
	return "\n\n".join(pages)


def _rows_to_markdown(rows):
	rows = [r for r in rows if any(str(c).strip() for c in r)]
	if not rows:
		return ""
	width = max(len(r) for r in rows)
	norm = [[("" if c is None else str(c)).replace("|", "/").replace("\n", " ") for c in r] + [""] * (width - len(r)) for r in rows]
	lines = ["| " + " | ".join(norm[0]) + " |", "|" + "---|" * width]
	lines += ["| " + " | ".join(r) + " |" for r in norm[1 : MAX_SHEET_ROWS + 1]]
	if len(norm) - 1 > MAX_SHEET_ROWS:
		lines.append(f"... ({len(norm) - 1 - MAX_SHEET_ROWS} more rows)")
	return "\n".join(lines)


def _xlsx_text(content):
	import openpyxl

	workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
	sheets = []
	for sheet in workbook.worksheets:
		rows = [list(r) for r in sheet.iter_rows(values_only=True, max_row=MAX_SHEET_ROWS + 2)]
		table = _rows_to_markdown(rows)
		if table:
			sheets.append(f"### Sheet: {sheet.title}\n{table}")
	return "\n\n".join(sheets)


def _docx_text(content):
	with zipfile.ZipFile(io.BytesIO(content)) as z:
		xml = z.read("word/document.xml").decode("utf-8", errors="replace")
	xml = re.sub(r"</w:p>", "\n", xml)
	xml = re.sub(r"<w:tab/>", "\t", xml)
	return re.sub(r"<[^>]+>", "", xml).strip()


def prepare_attachments(file_urls, bot=None, config=None):
	"""Returns (attachment_context_text, images) for the current turn.
	attachment_context is stored on the message so later turns still see the
	file's content; images are only sent on this turn."""
	settings = frappe.get_cached_doc("Support AI Settings")
	max_chars = settings.get("max_attachment_chars") or 8000
	use_google = bool(bot and bot.use_google_document_ai and settings.get("enable_google_document_ai"))
	processor_id = bot.google_processor_id if bot else None
	vision = bool(config and config.provider in ("OpenAI", "OpenAI Compatible", "LM Studio", "Ollama", "LocalAI"))

	blocks, images = [], []
	for file_url in (file_urls or [])[:MAX_ATTACHMENTS]:
		file_doc = get_permitted_file(file_url)
		ext = get_extension(file_doc)
		label = f"Attached file '{file_doc.file_name}' (file_url: {file_doc.file_url})"

		try:
			if ext in IMAGE_EXTENSIONS and not use_google:
				content = file_doc.get_content()
				if isinstance(content, str):
					content = content.encode()
				if vision and len(content) <= MAX_IMAGE_BYTES:
					images.append({"mime": IMAGE_MIME[ext], "data": base64.b64encode(content).decode()})
					blocks.append(f"{label}: image sent to you directly - describe/read it if relevant.")
				else:
					blocks.append(f"{label}: image could not be read (too large, or no OCR/vision configured).")
				continue

			text = extract_text(file_doc, use_google=use_google, processor_id=processor_id)
		except Exception as e:
			frappe.log_error(title="Support AI: could not read attachment", message=frappe.get_traceback())
			blocks.append(f"{label}: could not be read ({e}).")
			continue

		if not text or not text.strip():
			blocks.append(f"{label}: no readable text found.")
			continue
		if len(text) > max_chars:
			text = text[:max_chars] + f"\n... (truncated, {len(text) - max_chars} more characters)"
		blocks.append(f"{label} content:\n{text}")

	return "\n\n".join(blocks), images
