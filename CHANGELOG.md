# Changelog

## 0.5.2+expodocs.1 (review candidate)

- SearchQuery and SearchDialog accept `start`/`count`. Explicit pages request
  DocuWare's calculated total and expose `total`, `start`, and `page_size`;
  legacy unbounded searches still follow next links lazily.
- Connection.stream_to_file writes chunked downloads to a sibling temporary
  file, checks Content-Length and optional expected size, and atomically
  publishes only a complete file. HTTP responses and partial files are cleaned
  up on failure; existing destinations are preserved.
- OAuth token acquisition time and lifetime survive saved-state restoration.
  Connection and Client expose `is_token_expired()` and explicit `relogin()`;
  authentication failures propagate instead of being swallowed.
- Keep the distribution `docuware-client` and Python import `docuware`.
  No PyPI publication. Proposed tag: `v0.5.2+expodocs.1`, to be created only
  after external review on the approved commit. ExpoDocs must pin that full SHA.
- Make the upstream naive-date tests timezone-portable while retaining a
  fixed UTC epoch assertion. Update only Pylint/Astroid within the existing
  development constraints to fix a false `collections.abc` import error.

## 0.5.2

Upstream base used by the ExpoDocs fork.
