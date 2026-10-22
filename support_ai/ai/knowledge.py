# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Knowledge-base File Search for a bot.

- OpenAI + Agents SDK: files are uploaded to an OpenAI vector store and the
  hosted FileSearchTool is used.
- Every other provider: files are chunked and searched locally with a small
  BM25 ranker - no embeddings, no external service, works with Ollama."""

import math
import re
from collections import Counter

import frappe
from frappe import _

CHUNK_CHARS = 900
CHUNK_OVERLAP = 150
TOP_K = 4
CACHE_SECONDS = 24 * 60 * 60

STOPWORDS = set(
	"the a an and or of to in on for is are was were be by with as at it this that from what how why "
	"which who when where can do does did not no yes i you we they he she my our your".split()
)

SEARCH_TOOL_SCHEMA = {
	"type": "object",
	"properties": {
		"query": {"type": "string", "description": "What to look for, in a few keywords or a short question."}
	},
	"required": ["query"],
}


def _tokens(text):
	return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2 and t not in STOPWORDS]


def _chunk(text):
	text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
	chunks, start = [], 0
	while start < len(text):
		end = min(start + CHUNK_CHARS, len(text))
		# Prefer breaking on a paragraph/sentence boundary.
		if end < len(text):
			window = text.rfind("\n", start + CHUNK_CHARS // 2, end)
			if window == -1:
				window = text.rfind(". ", start + CHUNK_CHARS // 2, end)
			if window != -1:
				end = window + 1
		chunks.append(text[start:end].strip())
		if end >= len(text):
			break
		start = max(end - CHUNK_OVERLAP, start + 1)
	return [c for c in chunks if c]


def _file_chunks(file_url):
	from support_ai.ai.files import extract_text

	file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not file_name:
		return []
	file_doc = frappe.get_doc("File", file_name)
	key = f"support_ai:kb:{file_doc.name}:{file_doc.modified}"
	cached = frappe.cache.get_value(key)
	if cached is not None:
		return cached

	try:
		chunks = _chunk(extract_text(file_doc))
	except Exception:
		frappe.log_error(title="Support AI: could not index knowledge file", message=frappe.get_traceback())
		chunks = []
	frappe.cache.set_value(key, chunks, expires_in_sec=CACHE_SECONDS)
	return chunks


def search_knowledge(bot, query, top_k=TOP_K):
	"""Knowledge files are configured by a System Manager on the bot, so any
	user allowed to use the bot may search them."""
	query_terms = set(_tokens(query))
	if not query_terms:
		return {"results": [], "note": "Empty query."}

	docs = []
	for row in bot.knowledge_files or []:
		for i, chunk in enumerate(_file_chunks(row.file)):
			docs.append({"file": row.file_name or row.file, "chunk": i, "text": chunk, "tokens": Counter(_tokens(chunk))})
	if not docs:
		return {"results": [], "note": "The knowledge base has no readable files."}

	n = len(docs)
	avg_len = sum(sum(d["tokens"].values()) for d in docs) / n or 1
	df = Counter(t for d in docs for t in set(d["tokens"]) if t in query_terms)
	k1, b = 1.5, 0.75

	for d in docs:
		length = sum(d["tokens"].values()) or 1
		score = 0.0
		for t in query_terms:
			tf = d["tokens"].get(t, 0)
			if not tf:
				continue
			idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
			score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg_len))
		d["score"] = score

	hits = sorted((d for d in docs if d["score"] > 0), key=lambda d: d["score"], reverse=True)[:top_k]
	if not hits:
		return {"results": [], "note": f"Nothing in the knowledge base matched {query!r}."}
	return {
		"results": [{"file": h["file"], "excerpt": h["text"], "score": round(h["score"], 2)} for h in hits],
		"note": "Answer from these excerpts and name the file you used.",
	}


# --- OpenAI vector store ------------------------------------------------------------


def uses_openai_vector_store(bot, config):
	return bool(
		bot.enable_file_search and bot.engine == "OpenAI Agents SDK" and config.provider == "OpenAI"
	)


def _openai_client():
	from openai import OpenAI

	settings = frappe.get_cached_doc("Support AI Settings")
	api_key = settings.get_password("openai_api_key", raise_exception=False)
	if not api_key:
		frappe.throw(_("OpenAI API Key is not configured in Support AI Settings."))
	return OpenAI(
		api_key=api_key,
		organization=settings.get("openai_organisation_id") or None,
		project=settings.get("openai_project_id") or None,
	)


def sync_openai_vector_store(bot_name):
	"""Uploads new knowledge files to the bot's OpenAI vector store and removes
	files that were taken off the bot. Runs in the background after save."""
	bot = frappe.get_doc("Support AI Bot", bot_name)
	client = _openai_client()

	if not bot.openai_vector_store_id:
		store = client.vector_stores.create(name=f"Support AI - {bot.name}")
		bot.db_set("openai_vector_store_id", store.id)

	keep = set()
	for row in bot.knowledge_files:
		if row.openai_file_id:
			keep.add(row.openai_file_id)
			continue
		file_name = frappe.db.get_value("File", {"file_url": row.file}, "name")
		if not file_name:
			continue
		file_doc = frappe.get_doc("File", file_name)
		uploaded = client.files.create(file=(file_doc.file_name, file_doc.get_content()), purpose="assistants")
		client.vector_stores.files.create(vector_store_id=bot.openai_vector_store_id, file_id=uploaded.id)
		row.db_set("openai_file_id", uploaded.id)
		keep.add(uploaded.id)

	for vs_file in client.vector_stores.files.list(vector_store_id=bot.openai_vector_store_id):
		if vs_file.id not in keep:
			client.vector_stores.files.delete(vector_store_id=bot.openai_vector_store_id, file_id=vs_file.id)
			try:
				client.files.delete(vs_file.id)
			except Exception:
				pass
