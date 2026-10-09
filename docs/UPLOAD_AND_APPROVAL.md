# Book upload and approval guide

## Who does what

| Role | Can |
|---|---|
| Contributor | register resources, upload files to their own drafts, submit, revise after rejection or a request for information |
| Librarian | everything a contributor can, plus review, approve, reject, request information, publish, withdraw, archive, edit any record and its access policy, manage entitlements, retry jobs, read the audit log; verify rights unless `LIBRARIAN_CAN_VERIFY_RIGHTS=false` |
| Rights manager | verify or reject rights, edit access policies, manage entitlements, review, withdraw |
| Platform administrator | all of the above, plus user roles, institutions and permanent deletion |

Small library: one librarian can do every step. Institution: set `REQUIRE_SEPARATE_REVIEWER=true` and the person who submitted a resource can no longer review it or verify its rights.

## The workflow

```
DRAFT ──submit──▶ SUBMITTED ──start review──▶ UNDER_REVIEW ──approve──▶ APPROVED ──publish──▶ PROCESSING ──▶ PUBLISHED
  ▲                 │   │                        │    │                                         │
  │                 │   └─reject (reason)────────┴────┼──▶ REJECTED ──revise──▶ DRAFT           └─▶ PROCESSING_FAILED ──retry──▶ PROCESSING
  └──(resubmit)── NEEDS_INFORMATION ◀─request info (reason)┘
PUBLISHED / APPROVED / PROCESSING_FAILED ──withdraw (reason)──▶ WITHDRAWN ──reinstate (reason)──▶ UNDER_REVIEW
WITHDRAWN / REJECTED / DRAFT ──archive──▶ ARCHIVED ──(platform admin) permanent delete
```

Only these transitions exist; the server rejects anything else (`409`), and a status can never be set by editing a record. Every transition records who, when, from, to and the reason (`publication_events`), and review decisions are kept separately (`book_reviews`).

## Step by step (Manage → Add resource)

1. **Register the record.** Title and language are required. Fill in authors, ISBN, publisher, year, edition, subject, categories and a description; the description is what people who cannot read the book will see in a discoverable catalogue.
2. **Record the rights.** Choose the rights basis, licence, licence URL, rights holder, and the evidence in *rights notes*. Set the three licence flags: display, download, machine processing.
3. **Choose the access policy** (librarians and rights managers; contributors' drafts stay `UNPUBLISHED` until review). See [RIGHTS_AND_ACCESS.md](RIGHTS_AND_ACCESS.md).
4. **Upload the file** (PDF or UTF-8 text, up to 50 MB), or leave it as a catalogue-only / external-provider record. The server checks the bytes (not the extension), computes SHA-256, rejects exact duplicates, stores it under a random key outside the web root, and records who uploaded it. Malware scanning is not configured (shown as `no scanner configured`).
5. **Submit for review.**
6. **Review.** A reviewer starts the review, downloads the file for inspection if needed (audited), and then approves, rejects (reason required) or requests information (reason required).
7. **Verify rights.** Someone with the rights permission marks rights verified (or rejected). Approval is blocked until rights are verified and every publication rule passes; the page lists what is missing.
8. **Publish.** This moves the resource to `PROCESSING` and queues one processing job through the transactional outbox. The worker validates again, checks the stored file's checksum, extracts text page by page (in an isolated process with a time limit), runs OCR on pages without a text layer if Tesseract is installed and the licence allows machine processing, detects chapters (PDF outline or headings), chunks, embeds and indexes. Only when all of that succeeds does the resource become `PUBLISHED`. Failures show the exact error on the resource and in *Processing jobs*.
9. **Grant access** for restricted policies under *Entitlements*: a person (by email), a group or an institution, optionally with an expiry.

## After publication

- **Replace the file:** upload a new one on the published resource; it is reprocessed and the old content stays live until the new run succeeds.
- **Reprocess / re-index:** *Reprocess and re-index* on the resource, or `make reindex` for everything.
- **Change the policy:** edit it; it applies on the next request, and search caches are invalidated.
- **Withdraw:** reason required. The resource disappears from the catalogue, reader, downloads and search immediately (results are re-checked against the database even before the index is rebuilt). Its history and audit log remain.
- **Archive, then delete permanently:** platform administrators only; the files are removed from storage, the audit trail is kept.

## When processing fails

| Message | What to do |
|---|---|
| `no text layer and OCR is not installed` | install Tesseract, or upload a PDF with a text layer |
| `no text layer, and the licence does not allow OCR` | turn on machine processing if the licence allows it, or upload a text-layer PDF |
| `the PDF is encrypted or DRM-protected` | upload an unprotected copy you are permitted to use |
| `stored file failed its checksum` | the stored file was damaged: upload it again, then retry |
| `not publishable: ...` | fix the listed record problem, then retry |
| `worker stopped while processing` | recovered automatically (re-queued up to 3 attempts); retry if it finally failed |
