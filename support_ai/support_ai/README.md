<!-- Copyright (c) 2026, shyam8848 and contributors -->
<!-- For license information, please see license.txt -->

# Support AI (module)

This module holds the Support AI DocTypes, reports, page, scheduled jobs
and chat API.

## DocTypes

| DocType | Purpose |
| ------- | ------- |
| Support AI Settings | The LLM provider, the reasoning model, streaming, the rate limit, sensitive fields, synonyms, suggested questions, the daily digest, and code access (role and context size). |
| Support AI Bot | An assistant with its own model, instructions, functions and knowledge files. |
| Support AI Conversation | One chat thread of a user. |
| Support AI Message (child) | One message, with its answer source, response time, feedback and actions. |
| Support AI Function / Function Param (child) | Tools that a bot can call. |
| Support AI Action Log | An audit record of each data change that the AI made. |
| Support AI Prompt | Saved prompts. |
| Support AI Instruction Template | Reusable bot instructions. |
| Support AI Knowledge File (child) | The documents that a bot can search. |
| Support AI Allowed Role (child) | The roles that can use a bot or a function. |
| Support AI Bot Function (child) | The functions given to a bot. |
| Support AI Synonym (child) | A word or phrase that names a DocType, for example GRN = Purchase Receipt. |
| Support AI Suggested Prompt (child) | A one-click question, for all users or for one role. |

## Reports

| Report | Purpose |
| ------ | ------- |
| Support AI Usage | Answers per day: from the database or the AI model, errors, response time and feedback. |
| Support AI Unanswered Questions | Questions with a 👎, an error or no result. |

## Page

- **support-ai**: the full-page chat.

## API (`api/v1/assistant.py`)

- `set_feedback`, `export_records`, `get_suggestions`, `resolve_write_action`.

## Scheduled jobs (`tasks.py`)

- `send_daily_digest` (daily): emails a "what needs your attention"
  summary to the users with the digest role.
