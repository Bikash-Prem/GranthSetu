# Rights and access policies

Everything here is enforced by one server-side module, `backend/granthsetu/policy.py`. The frontend only displays its decisions.

## The decision

For every request the server loads, from PostgreSQL: the user's roles, their **current** institution and group memberships (expired ones ignored), their **active** entitlements (not revoked, not expired), and the resource's status, policy, catalogue setting and licence flags. It then answers one of five questions:

| Action | Covers |
|---|---|
| `discover` | the catalogue record (permitted metadata) in listings, search results and the detail page |
| `read` | pages, chapters, in-book search, full-text passages and snippets, reading progress and bookmarks |
| `ai` | putting content into a model prompt: explanations, lesson packs, practice questions (needs `read` **and** the licence's machine-processing flag) |
| `download` | the original file (needs `read` **and** the licence's download flag) |
| `manage` | staff views of a resource that is not published |

If the answer is "no", the response is **404** when the user may not even know the resource exists (indistinguishable from a missing id), **401** when signing in would help, otherwise **403** with a plain reason. Denials are written to the audit log, without content.

## Resource policies

| Policy | Who discovers it | Who reads it in GranthSetu |
|---|---|---|
| `OPEN_ACCESS` | everyone | everyone; downloads only if the licence flag allows |
| `PUBLIC_METADATA_ONLY` | everyone | only users with an explicit entitlement |
| `REGISTERED_USERS` | per catalogue setting | any signed-in user |
| `INSTITUTION_ONLY` | per catalogue setting | current members of the resource's institution, or entitled users/groups/institutions |
| `GROUP_RESTRICTED` | per catalogue setting | current members of the resource's group, or entitled users |
| `INDIVIDUAL_ENTITLEMENT` | per catalogue setting | users with an active entitlement (directly, via a group, or via an institution) |
| `EXTERNAL_PROVIDER_ACCESS` | everyone | nobody in GranthSetu: the card links to the provider's own access flow. This is a link, not an integrated reader |
| `PRIVATE` | staff with `resource.view_unpublished` only | staff, through the audited management views |
| `UNPUBLISHED` | staff only | staff only |

**Catalogue setting** (for the four gated policies): `discoverable` shows the permitted metadata (title, author, language, description, licence) to everyone, with no text; `private` hides the record completely from anyone who could not read it. Use `discoverable` only when the institution's agreements allow metadata to be shown.

A resource is visible to the public **only when its status is `PUBLISHED`**. Uploading a file never publishes anything, and visible metadata never makes a resource readable.

## Licence flags (what GranthSetu may do)

| Flag | Effect when off |
|---|---|
| `allow_display` | full text is never served, even to eligible users |
| `allow_download` | the original file is never served (default **off**) |
| `allow_ai` (machine processing) | no OCR, no full-text indexing, no embeddings, never sent to a model. The book can still be read page by page if its PDF has a text layer; it is discoverable only through its metadata |

## Rights basis

`public_domain` · `open_licence` · `institutional_licence` · `rights_holder_permission` · `external_provider` · `catalogue_only` · `unverified`.

Publication rules (checked at approval and again by the worker before publishing):

- rights must be **verified** by someone with `rights.verify`;
- `OPEN_ACCESS` needs `public_domain` or `open_licence`;
- `external_provider` and `catalogue_only` records cannot grant in-app reading and must not host a file;
- `EXTERNAL_PROVIDER_ACCESS` needs the provider URL; `INSTITUTION_ONLY` / `GROUP_RESTRICTED` need the institution / group;
- full-text policies need an uploaded file;
- editing any rights field resets rights to `unverified`, unless the editor can verify rights.

Things the system deliberately does **not** assume: that an administrator uploading a file may distribute it; that a web page anyone can view is open access; that a licence allows downloading, OCR, embedding or AI processing. Each is an explicit flag set during review. GranthSetu never bypasses paywalls, DRM, publisher logins or institutional access controls; encrypted PDFs are rejected.

## Entitlements

Granted per resource to a **user**, **group** or **institution**, optionally with an expiry. Revoking or expiring one takes effect on the next request, everywhere: reader, search, AI context, lesson packs, downloads, bookmarks list. Search caches cannot outlive a change, because every grant, revocation, membership change, policy change and withdrawal increments `policy_version`, which is part of every cache key.

## Harvested open records

Records harvested from open sources (Wikipedia, Gutenberg, DOAJ, ...) are `OPEN_ACCESS` with the licence reported by the source. Records that are behind a login or a price (OpenAlex subscription papers, Open Library borrowable books, Google Books for sale) are `EXTERNAL_PROVIDER_ACCESS` with only their published abstract or description stored. A librarian can withdraw any harvested record; later harvests never re-publish it.
