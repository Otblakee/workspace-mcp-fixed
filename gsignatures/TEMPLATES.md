# Signature templates: constraints for the branded versions

This file is the brief for whoever produces the branded HTML signatures. The
files shipped under `templates/` today are plain placeholders that prove the
pipeline; the branded files replace them one entity at a time by bumping the
pinned version in `config/entities.yaml`.

## Placeholders

A signature template (`<CODE>-signature-vX.Y.Z.html`) may use only:

| Placeholder | Value | Notes |
| --- | --- | --- |
| `{{name}}` | Full name from the Directory | Required |
| `{{title}}` | Job title from the Directory | Required |
| `{{mobile}}` | Mobile number from the Directory | Optional: must sit inside `{{#mobile}} ... {{/mobile}}` |
| `{{email}}` | The send-as address this signature is attached to | Required |
| `{{statutory}}` | The rendered statutory partial for the entity | Inserted as HTML, not escaped |

A statutory partial (`<CODE>-statutory-vX.Y.Z.html`) may use only:
`{{legal_name}}`, `{{trading_name}}`, `{{company_number}}`,
`{{place_of_registration}}`, `{{registered_office}}`, `{{vat_number}}`,
`{{phone}}`, `{{website}}`, `{{email}}` (the entity's contact address).

Every value is HTML-escaped when it is inserted, except `{{statutory}}`,
which is already HTML. Do not put HTML in the Directory expecting it to
render.

## Optional blocks

Wrap anything that depends on an optional value in a block:

```html
{{#mobile}}<tr><td>Mobile: {{mobile}}</td></tr>{{/mobile}}
```

The block, including its markers, is removed entirely when the value is
empty and kept when it is set. Blocks may not nest. A bare `{{mobile}}`
outside a block fails the render when the Directory has no mobile: the
engine refuses to leave a blank line rather than guess.

Unknown placeholder names fail the render. A signature template that uses a
statutory-only name (or the other way round) fails the render.

## Layout rules

* One `<table>` per signature. Table-based layout only. Gmail's signature
  editor and most mail clients are unreliable with `div` layouts and CSS
  positioning.
* All CSS inline on the element (`style="..."`). No `<style>`, `<script>`,
  `<link>`, `<meta>` or form elements anywhere in the file.
* Expect Gmail to strip HTML comments, `class` and `id` attributes,
  `position`, `float`, `display:none` and anything hidden. Treat this list as
  provisional until the live pilot confirms it, and record what Gmail
  actually kept or dropped here once known.
* Web-safe fonts with fallbacks (for example
  `font-family: Arial, Helvetica, sans-serif`). Web fonts are not loaded in
  signatures.
* Keep the whole rendered signature under 10,000 characters. Gmail enforces
  a 10,000-character limit on a signature, and the engine refuses a render
  over that size (`engine.MAX_SIGNATURE_CHARS`): the address becomes an
  `error` row naming the template and the size, and nothing is sent.
* Images only by absolute public `https://` URL on a host we control (not a
  Drive link, not a Docs-published link, not a data URI). Every image needs
  `alt` text and explicit `width` and `height` attributes.
* No tracking pixels.

## Where the signature is seen

* Gmail on the web uses this signature.
* The Gmail mobile apps use it too, unless the user has set a separate
  plain-text mobile signature in the app; the app setting wins. Mobile
  numbers are optional in the Directory and the signature simply omits the
  row when there is none.
* Apple Mail, Outlook and any other client that talks to Gmail over IMAP or
  a connector never see it. That is a known limitation and is not solved by
  this feature.

## Files and versions

* Signature: `templates/<CODE>/<CODE>-signature-vX.Y.Z.html`
* Statutory partial: `templates/<CODE>/<CODE>-statutory-vX.Y.Z.html`
* Versions are semver (`X.Y.Z`). The version in the filename must match the
  version pinned in `config/entities.yaml` (`template_version` and
  `statutory_version`), or the config fails to load.
* To roll out a new template, add the new file alongside the old one and
  bump the pinned version. Never edit a file in place under an existing
  version: the ledger records which version was applied, and drift
  detection depends on that being true.
* The statutory partial is one file per entity so that a legal change (a
  new registered office, a VAT number) is one edit and one version bump for
  that entity only.
* AHWE (Art History with Emily) is ring-fenced: its own template directory,
  no group branding, and nothing in its files may mention any group company,
  address or brand. A test enforces this.

## Checklist before handing a template over

1. Renders with the engine for a person with a mobile and for one without,
   with no `{{` left in the output and no empty row either way.
2. Uses only the placeholders listed above; every optional value is inside a
   block.
3. Exactly one `<table>`; all CSS inline; none of `<style>`, `<script>`,
   `<link>`, `<meta>`, `<form>`.
4. Fonts are web-safe with fallbacks.
5. Every image is an absolute `https://` URL on our host, with `alt`,
   `width` and `height`.
6. Rendered size is under 10,000 characters for the longest name, title and
   mobile in the Directory (the engine refuses anything over the limit at
   apply time, so a template near the limit fails for some people only).
7. The statutory partial carries the legal name, place of registration,
   company number and registered office for a limited company, and the
   values in `entities.yaml` have been checked against Companies House
   (`statutory_verified: true`).
8. The file is saved under the right name and version, and the version is
   pinned in `entities.yaml`.
9. `pytest tests/gsignatures` passes.
10. Applied to one test mailbox first (dry run, then live), and the read-back
    from Gmail checked by eye before the entity-wide rollout.
