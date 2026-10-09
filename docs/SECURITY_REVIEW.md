# Security review

Scope: the code in this repository as of the platform release. This is an internal engineering review, **not** a penetration test or a certification. No compliance standard is claimed.

| Threat | Controls in place | Evidence | Residual risk |
|---|---|---|---|
| Unauthorised book access | single `policy.decide()` on every resource route; private/unpublished resources answer 404 like missing ids | `test_access_matrix_on_every_surface` (9 policies × 7 kinds of user × detail, pages, in-book search, catalogue, download); acceptance B, C | logic bugs in new endpoints that forget to call `decide()`; review new routes against this rule |
| Insecure direct object references | ids are checked per request; bookmarks/progress filtered by user id; staff file download needs a review permission | matrix test; `test_progress_and_bookmarks_follow_access` | — |
| Privilege escalation | roles only via platform-admin endpoints or the CLI; client-sent `roles`, `status`, `created_by` fields are ignored; last admin cannot be removed | `test_register_login_logout_and_cookie_csrf`, `test_illegal_transitions_and_permissions`, `test_last_admin_cannot_be_removed…` | — |
| Stale entitlements, expired licences | memberships and entitlements re-read with expiry on every request; `policy_version` in every search cache key | `test_entitlement_grant_then_revoke…`, `test_membership_expiry`, acceptance B | in-memory index lags by seconds; covered by the per-request DB re-check |
| Search-index leakage | access mask applied before BM25/FAISS ranking; restricted full text never scored for unauthorised users; second-column cards use metadata chunks only | `test_search_snippets_and_ai_context_never_leak`; mutation test (breaking one policy rule makes it fail) | — |
| Unauthorised AI retrieval, summaries as a loophole | only `read` + `allow_ai` passages enter prompts; lesson packs check `ai` per resource | the same test spies on every prompt sent to the model | — |
| Prompt injection through documents | prompts mark documents as untrusted; delimiter tags stripped from document text; every generated sentence must quote its passage or is dropped; invented passage ids discarded | `test_prompt_injection_in_a_book_is_neutralised` | a model can still be misled into a quoted but misleading sentence; the UI shows quotes and sources |
| Malicious uploads, unsafe parsing | type from bytes not extension; size limit at gateway and API; PDF parsing in a separate process with a time limit and crash detection; page limit; encrypted PDFs refused | `test_upload_validation…`, `test_unsupported_and_encrypted_files_fail_honestly` | **no malware scanner** (`scan_status = not_configured`); pypdf/Tesseract vulnerabilities; add ClamAV and resource limits (cgroups) for production |
| Path traversal, public exposure of private files | random 32-hex storage keys validated by regex and resolved inside the root; `O_EXCL` writes; storage outside the web root; files only via authorised endpoints; paths never returned | `test_upload_validation_duplicates_and_storage_safety` | the file store is not encrypted at rest; use an encrypted volume |
| SQL injection | parameterised queries everywhere; the few dynamic column lists are built from fixed whitelists | code review | — |
| XSS | React escapes output; no `dangerouslySetInnerHTML`; book text rendered as text; `X-Content-Type-Options: nosniff` and `Content-Disposition: attachment` on downloads | code review | no Content-Security-Policy header yet (add one at the gateway) |
| CSRF | session cookie `HttpOnly`, `SameSite=Strict`; cookie-authenticated writes require the `X-GranthSetu-CSRF` header; CORS closed unless `CORS_ORIGINS` is set | `test_register_login_logout_and_cookie_csrf` | — |
| Authentication weaknesses | scrypt (N=2^14, r=8); 10-character minimum; constant-work check for unknown emails; generic errors; server-side sessions storing only token hashes; logout, password change (signs out other sessions), deactivation | auth tests | no MFA, no email verification, no password reset by email |
| Rate-limit abuse | per-IP limits on search, scan, ingest, login (per IP and per email), registration, password change, uploads; nginx `limit_req` | `test_rate_limit…` (core suite) | limits are fixed-window; a botnet can spread load |
| Secret exposure | secrets only from environment; compose refuses to start without database passwords; example automation tokens refused; no default admin; `.env` gitignored | `test_automation_token_rejects_example_values` | — |
| Cache leakage across users | signed-in responses get `Cache-Control: private, no-store` and `Vary: Cookie, Authorization`; the gateway micro-cache bypasses and never stores requests with a session cookie or Authorization header | config review | gateway config not exercised in our environment (no nginx/Docker) |
| Audit and privacy | audit rows record actor, action, target, outcome, never content, passwords or queries; scan photos never stored; search queries not linked to accounts; users can delete their reading data | audit assertions in tests | audit log retention must be set by the operator |

## Known gaps to close before production

1. Malware scanning (e.g. ClamAV `clamd` INSTREAM) before processing.
2. Content-Security-Policy and HSTS headers at the gateway; HTTPS with `COOKIE_SECURE=true`.
3. Password reset and email verification flows; optional MFA for staff.
4. Encryption at rest for the file volume and database backups.
5. Shared object storage (S3/Spaces) for multi-host deployments.
6. External penetration test.
