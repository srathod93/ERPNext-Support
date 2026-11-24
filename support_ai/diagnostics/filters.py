# Copyright (c) 2026, shyam8848 and contributors
# For license information, please see license.txt

"""Turns wording into list filters for the router: date ranges ("last 7
days", "this quarter", "between 1 Jan and 15 Feb", "this financial year"),
amount conditions ("above 1 lakh"), link values ("for company Jio", "of
supplier Goodedge") and item filters ("invoices containing item X"). Every
value lookup goes through frappe.get_list, so the asking user's permissions
apply."""

import calendar
import datetime
import difflib
import re

import frappe
from frappe.utils import add_days, add_months, cint, flt, getdate, nowdate

MONTHS = [m.lower() for m in calendar.month_name[1:]]
MONTH_ABBR = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTH_PATTERN = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"

UNITS = {
	"k": 1_000,
	"thousand": 1_000,
	"lakh": 100_000,
	"lakhs": 100_000,
	"lac": 100_000,
	"lacs": 100_000,
	"l": 100_000,
	"cr": 10_000_000,
	"crore": 10_000_000,
	"crores": 10_000_000,
	"m": 1_000_000,
	"million": 1_000_000,
	"mn": 1_000_000,
}
AMOUNT_RE = re.compile(
	r"\b(above|over|more than|greater than|exceeding|at least|min(?:imum)?|below|under|less than|upto|up to|"
	r"at most|max(?:imum)?)\s*(?:rs\.?|inr|₹|\$)?\s*([\d,]+(?:\.\d+)?)\s*(k|thousand|lakhs?|lacs?|l|cr|crores?|"
	r"m|million|mn)?\b",
	re.IGNORECASE,
)
AMOUNT_FIELDS = ("grand_total", "base_grand_total", "total", "paid_amount", "total_debit", "amount")

# Text dates: 2026-01-15, 15/01/2026, 15-01-2026, "15 jan 2026", "jan 15 2026", "15 jan".
DATE_PATTERNS = (
	(re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
	(re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b"), "dmy"),
	(
		re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{MONTH_PATTERN}\.?,?\s*(\d{{4}})?\b", re.IGNORECASE),
		"d_mon",
	),
	(
		re.compile(rf"\b{MONTH_PATTERN}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})?\b", re.IGNORECASE),
		"mon_d",
	),
)


def _tokens(text):
	"""Lowercase alphanumeric words."""
	return re.findall(r"[a-z0-9]+", (text or "").lower())


def match_month(token, stop_words=()):
	"""Month number for a month name/abbreviation, tolerating typos ("augest")."""
	if token in MONTH_ABBR and len(token) >= 3:
		return MONTH_ABBR[token]
	if token == "sept":
		return 9
	if token in MONTHS:
		return MONTHS.index(token) + 1
	if len(token) >= 5 and token not in stop_words:
		close = difflib.get_close_matches(token, MONTHS, n=1, cutoff=0.75)
		if close and close[0][0] == token[0]:
			return MONTHS.index(close[0]) + 1
	return None


def _month_from_text(text):
	"""Month number from a matched month word (first 3 letters)."""
	return MONTH_ABBR.get(text[:3].lower())


def _find_text_dates(text):
	"""[(date, matched_text)] for explicit dates in the text, in order."""
	found = []
	taken = []
	for pattern, kind in DATE_PATTERNS:
		for m in pattern.finditer(text):
			if any(m.start() < end and m.end() > start for start, end in taken):
				continue
			try:
				if kind == "ymd":
					d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
				elif kind == "dmy":
					d = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
				elif kind == "d_mon":
					year = int(m.group(3)) if m.group(3) else getdate(nowdate()).year
					d = datetime.date(year, _month_from_text(m.group(2)), int(m.group(1)))
				else:
					year = int(m.group(3)) if m.group(3) else getdate(nowdate()).year
					d = datetime.date(year, _month_from_text(m.group(1)), int(m.group(2)))
			except TypeError, ValueError:
				continue
			taken.append((m.start(), m.end()))
			found.append((m.start(), d, m.group(0)))
	return [(d, s) for _pos, d, s in sorted(found)]


def _fiscal_year(date):
	"""(start, end, name) of the fiscal year containing date, or None."""
	if "erpnext" not in frappe.get_installed_apps():
		return None
	try:
		from erpnext.accounts.utils import get_fiscal_year

		name, start, end = get_fiscal_year(date)[:3]
		return getdate(start), getdate(end), name
	except Exception:
		return None


def _fmt(date):
	"""'01 Sep 2026' style label for a date."""
	return date.strftime("%d %b %Y")


def _quarter(date):
	"""(start, end) of the calendar quarter containing date."""
	q_start_month = 3 * ((date.month - 1) // 3) + 1
	start = datetime.date(date.year, q_start_month, 1)
	end = add_days(add_months(start, 3), -1)
	return start, getdate(end)


def detect_date_range(tokens, text="", stop_words=()):
	"""Returns ((from_date, to_date), label, consumed_tokens) or (None, None, set()).

	Parameters:
		tokens (list[str]): Lowercase words of the message.
		text (str): The message text (for explicit dates like 15/01/2026).
		stop_words (iterable): Words that are never months (typo matching).
	"""
	today = getdate(nowdate())
	joined = " " + " ".join(tokens) + " "

	# Explicit dates first: "between X and Y", "from X to Y", "since X", "before X", "on X".
	dates = _find_text_dates(text) if text else []
	if dates:
		consumed = set()
		for _d, s in dates:
			consumed |= set(_tokens(s))
		consumed |= {"between", "from", "to", "and", "since", "after", "before", "till", "until", "on"}
		if len(dates) >= 2:
			a, b = sorted([dates[0][0], dates[1][0]])
			return (a, b), f"{_fmt(a)} to {_fmt(b)}", consumed
		d = dates[0][0]
		if any(f" {w} " in joined for w in ("since", "after", "from")):
			return (d, today), f"since {_fmt(d)}", consumed
		if any(f" {w} " in joined for w in ("before", "till", "until", "upto")):
			return (datetime.date(1900, 1, 1), d), f"up to {_fmt(d)}", consumed
		return (d, d), f"on {_fmt(d)}", consumed

	m = re.search(r" (?:last|past|previous) (\d+) (day|days|week|weeks|month|months|year|years) ", joined)
	if m:
		n, unit = cint(m.group(1)), m.group(2)
		if unit.startswith("day"):
			start = add_days(today, -n + 1)
		elif unit.startswith("week"):
			start = add_days(today, -7 * n + 1)
		elif unit.startswith("month"):
			start = add_months(today, -n)
		else:
			start = add_months(today, -12 * n)
		return (getdate(start), today), f"last {n} {unit}", {"last", "past", "previous", m.group(1), unit}

	if "today" in tokens:
		return (today, today), "today", {"today"}
	if "yesterday" in tokens:
		d = today - datetime.timedelta(days=1)
		return (d, d), "yesterday", {"yesterday"}

	week_start = today - datetime.timedelta(days=today.weekday())
	if " this week " in joined or " current week " in joined:
		return (week_start, today), "this week", {"this", "current", "week"}
	if " last week " in joined or " previous week " in joined:
		start = week_start - datetime.timedelta(days=7)
		return (start, start + datetime.timedelta(days=6)), "last week", {"last", "previous", "week"}

	if " this month " in joined or " current month " in joined:
		return (today.replace(day=1), today), "this month", {"this", "current", "month"}
	if " last month " in joined or " previous month " in joined:
		end = today.replace(day=1) - datetime.timedelta(days=1)
		return (end.replace(day=1), end), "last month", {"last", "previous", "month"}

	if " this quarter " in joined or " current quarter " in joined:
		start, _end = _quarter(today)
		return (start, today), "this quarter", {"this", "current", "quarter"}
	if " last quarter " in joined or " previous quarter " in joined:
		start, _end = _quarter(today)
		prev_start, prev_end = _quarter(start - datetime.timedelta(days=1))
		return (prev_start, prev_end), "last quarter", {"last", "previous", "quarter"}

	fy_words = {"financial", "fiscal", "fy", "year"}
	if re.search(r" (this|current) (financial year|fiscal year|fy) ", joined):
		fy = _fiscal_year(today)
		if fy:
			return (fy[0], fy[1]), f"financial year {fy[2]}", {"this", "current"} | fy_words
	if re.search(r" (last|previous) (financial year|fiscal year|fy) ", joined):
		this_fy = _fiscal_year(today)
		fy = this_fy and _fiscal_year(this_fy[0] - datetime.timedelta(days=1))
		if fy:
			return (fy[0], fy[1]), f"financial year {fy[2]}", {"last", "previous"} | fy_words

	if " this year " in joined or " current year " in joined:
		return (datetime.date(today.year, 1, 1), today), str(today.year), {"this", "current", "year"}
	if " last year " in joined or " previous year " in joined:
		y = today.year - 1
		return (datetime.date(y, 1, 1), datetime.date(y, 12, 31)), str(y), {"last", "previous", "year"}

	years = [cint(t) for t in tokens if re.fullmatch(r"(19|20)\d\d", t)]
	year = years[0] if years else None
	for t in tokens:
		month = match_month(t, stop_words)
		if month:
			y = year or today.year
			last_day = calendar.monthrange(y, month)[1]
			start, end = datetime.date(y, month, 1), datetime.date(y, month, last_day)
			return (start, end), f"{calendar.month_name[month]} {y}", {t, str(y)}
	if year:
		return (datetime.date(year, 1, 1), datetime.date(year, 12, 31)), str(year), {str(year)}

	return None, None, set()


def detect_amount_filter(meta, text):
	"""("grand_total", [">", 100000], label, consumed_tokens) or None, for
	wording like "above 1 lakh" / "less than 50k"."""
	m = AMOUNT_RE.search(text or "")
	if not m:
		return None
	field = next((f for f in AMOUNT_FIELDS if meta.has_field(f)), None)
	if not field:
		return None
	word, number, unit = m.group(1).lower(), m.group(2), (m.group(3) or "").lower()
	value = flt(number.replace(",", "")) * UNITS.get(unit, 1)
	is_min = word in ("above", "over", "more than", "greater than", "exceeding", "at least", "min", "minimum")
	op = (
		">="
		if word in ("at least", "min", "minimum")
		else ">"
		if is_min
		else "<="
		if word in ("upto", "up to", "at most", "max", "maximum")
		else "<"
	)
	label = f"{meta.get_label(field)} {op} {value:,.0f}"
	return field, [op, value], label, set(_tokens(m.group(0)))


def value_words(tokens, start, stop_words, limit=4):
	"""Words after position start that form a value ("goodedge advisory")."""
	words = []
	for t in tokens[start:]:
		if t in stop_words or len(words) >= limit:
			break
		words.append(t)
	return words


def search_link(doctype, words, limit=20):
	"""Names of `doctype` records whose ID or title contains the words, via
	get_list (so permissions apply). Returns [] when not permitted."""
	if not words or not frappe.has_permission(doctype, "read"):
		return []
	meta = frappe.get_meta(doctype)
	like = "%" + "%".join(words) + "%"
	or_filters = {"name": ["like", like]}
	title = meta.title_field or (frappe.scrub(doctype) + "_name")
	if meta.has_field(title):
		or_filters[title] = ["like", like]
	try:
		return frappe.get_list(doctype, or_filters=or_filters, pluck="name", limit_page_length=limit)
	except Exception:
		return []


def detect_link_filters(meta, tokens, stop_words, skip_fields=()):
	"""Filters for "for company jio" / "of supplier goodedge" on this DocType's
	Link fields. The first field linking to a named DocType wins, and one
	labelled exactly like its target DocType is tried first.

	Parameters:
		meta (Meta, required): Meta of the DocType being listed.
		tokens (list[str], required): Lowercase words of the message.
		stop_words (set, required): Words that end a value.
		skip_fields (iterable, optional): Fields already filtered.

	Returns:
		tuple: (filters, labels, consumed_tokens, errors)
	"""
	filters, labels, consumed, errors = {}, [], set(), []
	link_fields = [
		df
		for df in meta.fields
		if df.fieldtype == "Link"
		and df.options
		and not df.hidden
		and df.fieldname not in skip_fields
		and df.options not in ("DocType", "User", "Currency", "UOM", "Print Heading", "Letter Head")
	]
	link_fields.sort(key=lambda df: (df.label or "").lower() != df.options.lower())
	done_targets = set()
	for df in link_fields:
		if df.options in done_targets:
			continue
		key_words = _tokens(df.options)
		words = _value_after_phrase(tokens, key_words, stop_words)
		if not words:
			continue
		done_targets.add(df.options)
		consumed |= set(key_words) | set(words)
		names = search_link(df.options, words)
		if names:
			filters[df.fieldname] = ["in", names]
			shown = names[0] if len(names) == 1 else f"{names[0]} (+{len(names) - 1} more)"
			labels.append(f"{df.label or df.options} = {shown}")
		else:
			errors.append(f"No {df.options} matching '{' '.join(words)}' was found.")
	return filters, labels, consumed, errors


def _value_after_phrase(tokens, key_words, stop_words):
	"""Value words after the first occurrence of key_words that has any."""
	n = len(key_words)
	starts = [i for i in range(len(tokens) - n + 1) if tokens[i : i + n] == key_words]
	return next((w for w in (value_words(tokens, i + n, stop_words) for i in starts) if w), [])


def detect_item_filter(meta, tokens, stop_words):
	"""Child-table filter for "invoices containing item X": returns
	([[child_doctype, "item_code", "in", codes]], label, consumed) or None."""
	if "item" not in tokens and "items" not in tokens:
		return None
	item_table = next(
		(
			df.options
			for df in meta.fields
			if df.fieldtype == "Table" and df.options and frappe.get_meta(df.options).has_field("item_code")
		),
		None,
	)
	if not item_table:
		return None
	i = tokens.index("item") if "item" in tokens else tokens.index("items")
	words = value_words(tokens, i + 1, stop_words)
	if not words:
		return None
	codes = search_link("Item", words)
	label = f"Item = {codes[0]}" if len(codes) == 1 else f"Item like '{' '.join(words)}'"
	if not codes:
		return [[item_table, "item_code", "=", "__no_such_item__"]], label, {"item", "items", *words}
	return [[item_table, "item_code", "in", codes]], label, {"item", "items", *words}
