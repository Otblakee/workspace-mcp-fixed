# Security Audit & Multi-Entity Rollout Plan

Audit of `workspace-mcp-fixed` at commit `c014337`, performed ahead of
rolling the MCP out from a single user (`oliver@otbgroup.co.uk`) to OTB
Group staff across all four entities.

Method: full read of the auth / registration / audit core, a 6-dimension
parallel finder sweep (39 raw findings), adversarial verification of
findings (2 refuted and dropped), plus **live probes against the running
Render deployment** via the `OTB_DRIVE` MCP connector.

---

## 1. Executive summary

The codebase is in better shape than a typical fork. The prior hardening
waves were real: the OAuth 2.0 impersonation fallback is closed in OAuth 2.1
mode, the session store validates that a session can only fetch its own
credentials, SSRF protection on `fileUrl` is genuinely thorough (DNS
validation + IP pinning + redirects disabled), per-user audit attribution
works, and the destructive-tool denylist is **verified live** — none of the
14 blocked tools appear on the running service.

However, **it is not safe to onboard a second user today.** Three things
block it:

1. Google **refresh tokens and the OAuth client secret are stored as
   plaintext JSON** on the Render disk. Today that is one person's tokens.
   With 20 staff it becomes a single file-read that yields persistent,
   refreshable access to every mailbox and Drive in the group.
2. The domain restriction that is supposed to stop arbitrary Google accounts
   authenticating **is not enabled on the deployment** — `render.yaml` never
   sets `OAUTH_ALLOWED_EMAIL_DOMAINS`, and the code treats unset as
   "no restriction".
3. **There is no per-user authorization layer at all.** Every tool gate in
   the system (`BLOCKED_TOOLS`, tier filtering, `--read-only`) is evaluated
   once at process boot and applies identically to everyone. The permission
   control you asked for does not exist yet in any form.

There is also a **configuration drift** issue found only by probing the live
service: the deployment is running `gadmin` (Admin Directory tools + admin
scopes) even though `render.yaml` excludes it and `main.py` marks it
opt-in-only.

---

## 2. Findings

Severity is the post-verification severity where a verifier ran, otherwise
the finder's severity corroborated against my own read of the source.

### Blockers — fix before the second user

| # | Severity | Finding | Location |
|---|---|---|---|
| 1 | **Critical** | Refresh tokens + `client_secret` stored unencrypted at rest | `auth/credential_store.py:187` |
| 2 | **High** | `OAUTH_ALLOWED_EMAIL_DOMAINS` absent from deployment → any Google account may authenticate | `render.yaml:62`, `auth/auth_info_middleware.py:53` |
| 3 | **High** | No per-user authorization layer; all gating is process-global and boot-time | `core/server.py:143`, `core/tool_registry.py:31` |
| 4 | **High** | No token-revocation path — deleting a credential removes the local copy but never revokes at Google | `auth/credential_store.py:231` |
| 5 | **High** | Audit log is not tamper-evident: every writer holds Editor and can edit, delete or forge any row | `core/audit.py:513-522` |
| 6 | **High** | OAuth-proxy disk encryption key is derivable from `client_secret`, which is co-located on the same disk inside every token file | `core/server.py:419`, `auth/credential_store.py:192` |

### Important — fix during rollout

| # | Severity | Finding | Location |
|---|---|---|---|
| 7 | **High** | OAuth 2.0 mode trusts caller-supplied `user_google_email` and returns any cached user's credentials. *Latent*: unreachable while `MCP_ENABLE_OAUTH21=true`, live in CLI mode and if 2.1 is ever disabled | `auth/service_decorator.py:663` |
| 8 | **High** | `/attachments/{file_id}` is unauthenticated on an internet-facing server; storage is process-global and not tenant-scoped | `core/server.py:573` |
| 9 | **High** | Audit redaction is a key-name denylist that misses most PII-bearing params (`to`, `cc`, `bcc`, recipients, attendees) | `core/audit.py:55` |
| 10 | **Medium** | The `error` column bypasses redaction entirely and re-leaks values that `params_summary` redacts | `core/audit.py:683` |
| 11 | **Medium** | OAuth 2.0 callback path enforces no domain restriction at all — the policy is wired only into the two OAuth 2.1 gates | `auth/google_auth.py:517` |
| 12 | **Medium** | `MCP_SINGLE_USER_MODE` with a multi-user credential store returns the **alphabetically-first** user's credentials regardless of who was requested | `auth/google_auth.py:113` |
| 13 | **Medium** | `BLOCKED_TOOLS` is a denylist keyed on function name — fail-open for any new destructive tool nobody remembers to add | `core/tool_policy.py:29` |
| 14 | **Medium** | `--read-only` is a global boot flag, is not enabled in the production launch command, and cannot vary per user | `Dockerfile`, `core/tool_registry.py:142` |
| 15 | **Medium** | Credentials directory created with default permissions (`os.makedirs` without mode) | `auth/credential_store.py:127` |
| 16 | **Medium** | `client_secret` duplicated into every per-user token file and every in-memory session | `auth/credential_store.py:192` |
| 17 | **Medium** | SSRF: NAT64 prefix `64:ff9b::/96` passes `is_global` and could reach internal space where a NAT64 gateway exists | `gdrive/drive_tools.py:999` |

### Lower priority

| # | Severity | Finding | Location |
|---|---|---|---|
| 18 | Low | External-mode tokens stamped with the server's full `required_scopes`, making the per-tool scope gate non-discriminating | `auth/external_oauth_provider.py:128` |
| 19 | Low | PKCE definitively not applied on the legacy OAuth 2.0 flow (`autogenerate_code_verifier` never passed) | `auth/google_auth.py:384` |
| 20 | Low | In-memory OAuth 2.1 sessions have no idle or absolute TTL and no eviction | `auth/oauth21_session_store.py:256` |
| 21 | Low | Attachment writes lack `O_NOFOLLOW`/`O_EXCL`; `register_existing_file` trusts a caller-supplied absolute path with no containment check | `core/attachment_storage.py:111,194` |
| 22 | Low | User emails logged at INFO to application logs | `auth/google_auth.py:523` |
| 23 | Low | Unattributable calls silently recorded as `DEFAULT_USER` (`oli`), corrupting actor history | `core/audit.py:703` |
| 24 | Info | `validate_redirect_uri()` is dead code — `OAUTH_CUSTOM_REDIRECT_URIS` gives false assurance | `auth/oauth_config.py:194` |
| 25 | Info | CORS allowlist computed but never enforced | `auth/oauth_config.py:143` |
| 26 | Info | `SECURITY.md` is stale upstream boilerplate that misstates this deployment's posture | `SECURITY.md:25` |

### Refuted (verified false positives — recorded so they aren't re-raised)

- **ID token decoded with `verify_signature=False`** (`auth/google_auth.py:141`).
  The token arrives over TLS directly from Google's token endpoint in the
  same process that initiated the exchange; there is no attacker-controlled
  path to substitute it. Hygiene, not a vulnerability.
- **MCP session-id binding treated as bearer-equivalent**
  (`auth/auth_info_middleware.py:382`). The binding is immutable-first-writer
  and in-process; the described escalation is not reachable.

---

## 3. Live deployment findings

These came from probing the running Render service and are **not** visible
from the source alone.

**3.1 — Denylist confirmed working.** All 14 `BLOCKED_TOOLS` are absent from
the live tool surface. The registration-chokepoint design works as designed.

**3.2 — `gadmin` is live, contradicting `render.yaml`.** `list_orgunits` and
`list_groups` both return data from production. But `render.yaml` sets
`TOOLS = gmail drive calendar docs sheets contacts` (no `gadmin`) and
`main.py` places `gadmin` in `OPT_IN_TOOLS`. The Render dashboard has drifted
from the repo blueprint. This matters because `ADMIN_SCOPES` includes
`admin.directory.user.security`, which the source itself notes also
authorises `tokens.delete` — a write capability inside a module documented as
read-only by design.

**3.3 — Six OAuth client IDs all named "OTB Workspace MCP".** All under GCP
project `849437400675`. They are not equivalent: three hold identity scopes
only, one holds **full `drive` + `spreadsheets`**, and one holds ~30 scopes
including `documents`, `gmail.readonly`, `calendar.events` and
`admin.directory.orgunit.readonly`. Each is an independently valid
credential; revoking one does nothing to the other five. Consolidate to one
client and revoke the strays.

**3.4 — Nine unrelated grants hold scopes that bypass the MCP denylist.**
`invoce froward backfill`, `JIT_JohnDeere_Archiver_v1`, `OTB-Plaud-Router`,
`JIT FUEL CRISIS MONITOR` and `JIT Logistics — Hazmat Colli Auto-Pipeline`
each hold `https://mail.google.com/` — the full Gmail scope, including
permanent delete. `OTB_Folder_builder`, `Folder ID Pull all`, `Folder by
type` and `Temp folder builder 27/2/26` hold full `drive`.

This is the live instance of the caveat in `core/tool_policy.py`: gating the
MCP does not reduce token scope. Deletion is carefully blocked through the
MCP while nine Apps Script projects retain unrestricted delete rights on the
same mailbox and Drive.

---

## 4. The topology question (this changes the design)

The live probe settled what "multi-workspace" actually means here. It is
**one Google Workspace customer with multiple domains**, not multiple
tenants:

| Entity | Org Unit | Domain | Status |
|---|---|---|---|
| OTB Group | `/01 OTB` | `otbgroup.co.uk` | live (primary) |
| JIT Logistics | `/02 JIT` | `jit-logistics.com` | **live** |
| Vale Automotive | `/03 VALE` | `valeautomotive.co.uk` | staged under `otbgroup.co.uk`, pending cutover |
| BIR Developments | `/04 BIR` | `bir-d.co.uk` | staged under `otbgroup.co.uk`, pending cutover |

Plus `/99 _SYSTEM` (admin / breakglass) and `/Workspace Guests`.

**Two consequences.**

First, `CLAUDE.md` instructs setting `OAUTH_ALLOWED_EMAIL_DOMAINS=otbgroup.co.uk`
before adding a second user. **That value would lock out every JIT user
today.** Because `_claims_pass_domain_policy` prefers Google's `hd` claim,
and `hd` returns the user's own domain rather than the customer's primary
domain, a JIT user presents `hd=jit-logistics.com`, misses the allowlist and
is rejected. The correct value now is
`otbgroup.co.uk,jit-logistics.com`, and it must be updated again at each
domain cutover.

Second — and this is the good news — **you do not need multi-tenancy.**
Because the MCP always acts *as the calling user with that user's own OAuth
token*, Google's existing Drive/Gmail/Calendar ACLs are already the data
boundary. A JIT user physically cannot read a BIR Shared Drive through this
MCP unless Google already grants it. So the RBAC layer's job is
**capability control** (may this person delete? send mail? share
externally?) — *not* data isolation, which is already solved.

---

## 5. Plan

### Phase 0 — Blockers (before any second user)

1. **Encrypt the credential store at rest.** Wrap
   `LocalDirectoryCredentialStore` with Fernet using a key from a new
   `WORKSPACE_MCP_CREDENTIAL_ENCRYPTION_KEY` env var (Render secret, *not*
   derived from `client_secret` and not stored on the same disk). Migrate
   existing plaintext files on first read. Fixes #1 and breaks the #6 key
   -derivation chain.
2. **Stop writing `client_secret` into per-user token files** (#16). Inject
   it from config at load time instead.
3. **Set `OAUTH_ALLOWED_EMAIL_DOMAINS=otbgroup.co.uk,jit-logistics.com`** in
   the Render dashboard and in `render.yaml` (#2). Correct the wrong value
   documented in `CLAUDE.md`.
4. **Add a customer-ID check** as the durable control: resolve the caller via
   Admin Directory `users.get` and verify `customerId` matches OTB's. This
   survives every domain cutover without a config edit, and is strictly
   stronger than a domain string. Keep the domain allowlist as the cheap
   outer ring.
5. **Set the GCP OAuth client to Internal** if the project sits inside the
   Workspace org. This is the IdP-side control and the only one that holds
   if the app-layer check is ever misconfigured.
6. **Reconcile the Render `TOOLS` value with `render.yaml`** (#3.2) — decide
   deliberately whether `gadmin` should be live. If yes, put it in
   `render.yaml`; if no, remove it from the dashboard.
7. **`os.makedirs(self.base_dir, mode=0o700)`** (#15) — one line.
8. **Fix the audit `error` column** to run through `_redact` (#10) — small
   and stops a live PII leak into the sheet.

### Phase 1 — SSO hardening

9. Extend the domain/customer policy to the **OAuth 2.0 callback path** (#11),
   or remove the OAuth 2.0 path entirely now that 2.1 is the deployed mode.
   Removing it also kills #7 and #12 outright, which is the cleaner trade.
10. Add **idle + absolute TTL and eviction** to `OAuth21SessionStore` (#20).
11. Implement a **revocation endpoint** (#4): call Google's `oauth2/revoke`,
    delete the stored credential, and invalidate the session-store entry.
    Needed operationally the first time someone leaves the company.
12. Authenticate or shorten-lived the **`/attachments/` route** (#8) — bind
    the capability URL to the issuing user's session, or drop the route in
    favour of the inline/Drive-transfer pattern already used by
    `get_gmail_attachment_content` (see `FOLLOWUPS.md`).

### Phase 2 — Per-user RBAC (the main build)

The enforcement point must move from **boot time** to **call time**. Three
new pieces:

**`core/authz.py` — policy model.** Roles as capability sets:

| Role | Capabilities |
|---|---|
| `reader` | read/search/list tools only |
| `contributor` | + create, modify, comment (no send, no share, no soft-delete) |
| `publisher` | + `send_gmail_message`, external share tools |
| `steward` | + `soft_delete_drive_file` / `restore_drive_file` |
| `admin` | everything not in `BLOCKED_TOOLS` |

Policy expressed in a new `core/tool_roles.yaml`, **deny-by-default**: a tool
absent from every role is callable by nobody. That inverts the current
fail-open denylist posture (#13) without removing `BLOCKED_TOOLS`, which
stays as the absolute floor.

**Role source: Google Groups.** You have already modelled the org in groups —
`otb-exec@`, `otb-it-admins@`, `jit-ops@`, `jit-finance@`, `bir-hs@`,
`vale-workshop@` and ~46 more. Resolve caller → groups via Admin Directory
`list_user_groups`, map groups → roles in config, cache with a short TTL.
This means **permissions are administered in the Google Admin console, not
in code** — no redeploy to change someone's access, and it stays correct when
staff move between entities. (This is the one legitimate reason to keep
`gadmin` scopes enabled; decide #3.2 with that in mind.)

**`core/authz_middleware.py` — enforcement.** A FastMCP middleware alongside
`AuthInfoMiddleware`:
- `on_call_tool` — resolve identity → roles → check tool against policy →
  deny with a clear error before the tool runs.
- `on_list_tools` — filter the advertised surface per user, so people do not
  see tools they cannot call.

Both hooks are needed: filtering `list_tools` alone is cosmetic, and gating
`call_tool` alone leaves a confusing UI.

**Audit integration.** Add `role` and `decision` (allow/deny) columns so
denied attempts are recorded. Denials are the highest-signal rows in the
whole log.

### Phase 3 — Audit integrity

13. Move audit writes to a **service account** (or Workload Identity
    Federation) so users no longer need Editor on the sheet (#5, #9). This
    removes the cross-user read of other people's `params_summary` at the
    same time.
14. Mirror to **Render Postgres** with append-only grants, keeping Sheets as
    the human-readable view.
15. Convert redaction to an **allowlist** — log only params explicitly marked
    safe, rather than trying to enumerate every sensitive name (#9).

### Phase 4 — Workspace-side (outside this repo)

16. Consolidate the six OAuth clients to one; revoke the rest (#3.3).
17. Audit and revoke the nine broad third-party grants (#3.4) — particularly
    the five holding full Gmail scope and `Temp folder builder 27/2/26`.
18. Google Vault retention on Drive and Gmail — the only control that
    survives a token compromise.

---

## 6. Suggested sequencing

Phase 0 is roughly a day of work and is a hard gate — none of it is
optional before user #2. Phase 1 is a second day. Phase 2 is the real
project, perhaps 3–5 days including tests, and it is worth doing *before*
the wide rollout rather than after, because retrofitting permissions onto
users who already have unrestricted access is a much harder conversation
than granting them correctly the first time.

Phases 3 and 4 can run in parallel with the rollout.
