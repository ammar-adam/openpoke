# Selecting models

The application default is now `anthropic/claude-sonnet-4.6`. The previous hardcoded
`anthropic/claude-sonnet-4` ID was absent from the
[public OpenRouter model registry](https://openrouter.ai/api/v1/models) checked on
2026-10-28, while the 4.6 ID was listed. This is an explicit default change, not a
fallback after a failed request. Availability, pricing, and behavior can change;
choose the model deliberately rather than assuming versions are interchangeable.

Set `OPENPOKE_MODEL` in `.env` for a common model. Optional role overrides are:

| Role | Environment variable |
|---|---|
| Interaction Agent | `OPENPOKE_INTERACTION_MODEL` |
| Execution Agent | `OPENPOKE_EXECUTION_MODEL` |
| Execution Agent email search | `OPENPOKE_EXECUTION_SEARCH_MODEL` |
| Conversation summarizer | `OPENPOKE_SUMMARIZER_MODEL` |
| Email importance classifier | `OPENPOKE_EMAIL_CLASSIFIER_MODEL` |

Precedence is an explicitly supplied `Settings(...)` field, then its role variable,
then `OPENPOKE_MODEL`, then the documented default. A configured model ID is passed
through even if unlisted; the application does not silently replace it. An empty
model value already present in the process environment raises an error. The existing
`.env` loader ignores blank entries and never overwrites process environment values.

Settings are cached by the running server. Restart it after changing `.env` or
environment variables. Recorded evaluations explicitly supplied their model IDs;
this application default change does not relabel or alter those comparisons.
