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

**Two traps sit in the currently-documented plan, both of which would fire on
the day the second user is added and neither of which the current user can
trigger.** They are the most actionable findings here:

- The `OAUTH_ALLOWED_EMAIL_DOMAINS=otbgroup.co.uk` value that `CLAUDE.md`
  tells you to set before onboarding **locks out every account on a secondary
  domain** — confirmed against the live tenant, where
  `peter.wilce@jit-logistics.com` already exists (§4).
- The group lookup the authorization design depends on runs on the *caller's*
  credentials and requires the caller to be a Workspace admin. The first
  non-admin teammate would fail that lookup and, under deny-by-default, be
  locked out of every tool (§5.1).

Both are invisible today because the sole user is a super admin on the primary
domain. That is exactly why they would ship.

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
before adding a second user. **That value is a production lockout, not a
hardening measure**, and it would bite real people.

In a multi-domain customer, `hd` carries the *user's own* hosted domain, not
the customer's primary domain. Google's own auth library documents the claim
as "the hosted G Suite domain of **the user**", and each account is associated
with either the primary or one secondary domain. So a JIT user presents
`hd=jit-logistics.com`, misses a single-domain allowlist, and is rejected at
`auth_info_middleware.py:65` — or at line 72 via the email-domain fallback if
`hd` is absent.

This is not hypothetical. A read-only Directory query against the live tenant
found **`peter.wilce@jit-logistics.com`** (OU `/02 JIT/Compliance`) — a real
staff account whose primary email is on the secondary domain. Meanwhile the
current sole user signs in as `oliver@otbgroup.co.uk` and would *never* trip
the bug, which is precisely why it would ship undetected and surface only when
the second user is added. The rejection reason is logged and never returned to
the client, so it presents as an unexplained auth failure affecting JIT staff
only — about the hardest class of bug to diagnose remotely.

The same query surfaced a **third domain**: `oliver@otbgroup.co.uk` carries
aliases `otb@otbgroup.co.uk`, `oliver.blake@jit-logistics.com` and
**`oliver@blakefamily.uk`**. Whether `blakefamily.uk` is a secondary domain
(hosts its own sign-in accounts) or merely an alias domain (extra addresses
only, cannot be used to sign in) is unresolved. **Enumerate the domain list
from Admin console → Account → Domains, or `directory.domains.list`; do not
guess it.** On current evidence the minimum correct value is
`otbgroup.co.uk,jit-logistics.com`, plus `blakefamily.uk` if it is a sign-in
domain — and it must be updated at every domain cutover and every domain
addition.

One inference worth converting to fact first: Google has never published a
sentence stating outright that a secondary-domain user's `hd` is the secondary
domain. The conclusion follows from Google's library wording, the
primary/secondary account model, and universal operator experience — but before
this becomes the basis of a production allowlist, have Peter complete the OAuth
flow against a staging client and log the decoded `hd` and `email`. Five
minutes, and it removes the only unverified link in this chain.

Second — and this is the good news — **you do not need multi-tenancy.**
Because the MCP always acts *as the calling user with that user's own OAuth
token*, Google's existing Drive/Gmail/Calendar ACLs are already the data
boundary. A JIT user physically cannot read a BIR Shared Drive through this
MCP unless Google already grants it. So the RBAC layer's job is
**capability control** (may this person delete? send mail? share
externally?) — *not* data isolation, which is already solved.

### 4.1 Personal accounts are out of scope (decided)

An earlier draft of this plan weighed letting the owner's personal
`@gmail.com` account onto this deployment. That would have forced the GCP
OAuth client to stay **External**, because an Internal client cannot
authorise a consumer account — Google rejects it at the IdP. The app-layer
allowlist would then have become the only ring of defence rather than the
second.

That requirement has been removed. Personal Gmail now lives in a separate
project — `Otblakee/personal-gmail-mcp`, a single-user server running locally
over stdio, holding no organisational credentials and granting no
organisational access.

**Consequence for this deployment: it can and should go Internal.** Nothing
here needs to admit an account outside the Workspace customer, so the
strongest available control is now free to take. No capability is lost —
consumer accounts could never use Groups, `gadmin` or domain-wide delegation
anyway.

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
3. **Set the GCP OAuth client's audience to Internal.** This is now the
   *primary* control, not an outer ring — see item 5 for why. Google enforces
   customer membership at the authorization endpoint: a non-member is bounced
   with `403 org_internal` before any code, token or callback exists. It
   covers every domain in the tenant automatically, survives domain additions
   with no config change, and rejects an unmanaged consumer account holding
   one of your domain names — which no domain allowlist can do. Requires the
   GCP project to sit inside the Workspace organisation resource, so do it in
   the new project (§7.1).
4. **Set `OAUTH_ALLOWED_EMAIL_DOMAINS` to the fully enumerated domain list**
   (#2) — read from Admin console → Account → Domains, not assumed. See §4.
   Add it to the redeploy checklist as *"must be updated whenever a Workspace
   domain is added"*, and make a domain-policy rejection log loudly with the
   rejected domain, so the next lockout is diagnosed in seconds.
5. **Do not build the customer-ID check.** An earlier draft proposed resolving
   the caller via Admin Directory `users.get` and comparing `customerId`.
   That is not viable: no OIDC claim carries a customer ID (Google's discovery
   document lists neither it nor even `hd` in `claims_supported`), and
   `users.get` requires `admin.directory.user[.readonly]` — so every ordinary
   user would have to consent to admin-directory scopes and would still 403 on
   most calls. Internal (item 3) already enforces exactly this boundary, at
   the IdP, for free.

   Note the honest framing: **Internal and the domain allowlist are not
   concentric rings.** Internal admits the whole customer, which is correct;
   a domain list admits an enumerated subset, which is narrower and
   hand-maintained. Once the list is complete it is a redundant restatement of
   Internal that can only drift. Keep it as belt-and-braces, but do not
   describe it as the thing protecting the deployment.
6. **Reconcile the Render `TOOLS` value with `render.yaml`** (#3.2).
   **Decided: keep `gadmin`.** It is load-bearing for the authorization layer
   (§5 Phase 2) and worth building on. So add it to `render.yaml` rather than
   removing it from the dashboard — but move its scopes off individual users
   and onto the service account (§5.1), so ordinary staff never see an admin
   consent prompt.
7. **`os.makedirs(self.base_dir, mode=0o700)`** (#15) — one line.
8. **Fix the audit `error` column** to run through `_redact` (#10) — small
   and stops a live PII leak into the sheet.

### 5.1 The service account is the keystone

One component unlocks three otherwise-awkward problems, and it should be built
early rather than treated as a Phase 3 nicety: **a dedicated service account**,
created in the new GCP project (§7.1).

It solves:

1. **Role resolution for ordinary staff.** See the outage below — this is not
   an optimisation, it is the difference between the authorization layer
   working and locking everyone out.
2. **Admin scopes come off the user consent screen entirely.** `gadmin` is
   staying (Phase 0 item 6), but staff should consent to Gmail, Drive and
   Calendar only. The Admin SDK scopes — including
   `admin.directory.user.security`, which the source itself notes also
   authorises `tokens.delete` — then live solely with the service account.
3. **Audit-log integrity.** The service account becomes the audit writer, so
   users no longer need Editor on the sheet. That closes finding #5 and the
   cross-user read of other people's `params_summary` in one move, which is
   why Phase 3 item 13 should be pulled forward to here.

#### The outage this prevents

`list_user_groups` (`gadmin/admin_tools.py:339`) is decorated
`@require_google_service("admin_directory", …)`, so it calls
`groups().list(userKey=…)` **with the calling user's own credentials**. Its
docstring already concedes the constraint: *"must be a Workspace admin."*

The Admin SDK has no self-lookup exception — a non-admin gets `403 Not
Authorized` even holding the scope. (Google documents non-admin paths where
they exist: `users.get` has `viewType=domain_public`. Nothing equivalent
exists for groups.) Today this is invisible because the only user is a super
admin. **The moment a non-admin teammate is added, every authorization
decision for that user 403s — and under a deny-by-default policy that locks
them out of every tool.** The authz layer would fail closed on its own
identity lookup: a self-inflicted outage, not a security boundary.

#### Use direct role assignment, not domain-wide delegation

There are two ways to give the service account group-read access. They are not
equally safe:

- **Cloud Identity + direct role assignment (recommended).** Enable
  `cloudidentity.googleapis.com` and assign the **Group Administrator** role
  directly to the service account via the Admin SDK `roleAssignments` API
  (`assignedTo` = the SA's unique ID, `scopeType: CUSTOMER`). The SA then
  calls with its own credentials in admin authorization mode. **No
  impersonation, no delegation.**
- **Admin SDK + domain-wide delegation (avoid if possible).** Requires a
  `subject` — the SA must impersonate a real admin user. That grants the SA
  the ability to act *as* a user, which is a far larger blast radius than
  reading groups.

Prefer the first. Domain-wide delegation is the single most dangerous
credential in a Workspace tenant, and nothing here needs it.

#### Why not resolve groups with the user's own token

The Cloud Identity Groups API does have a documented non-admin mode
(`searchDirectGroups` with `cloud-identity.groups.readonly`), so this looks
tempting. It is a trap for an authorization layer:

- **It silently under-reports.** Per the API description, groups the caller
  lacks permission to view are *"silently filtered out"* — no error, no
  indicator. Role sets would be quietly incomplete, producing denials that
  depend on per-group visibility settings any group owner can change.
- **Nested groups are Enterprise-gated.** `searchTransitiveGroups` and friends
  return 403 below Enterprise Standard / Cloud Identity Premium. If OTB is on
  Business Standard/Plus — likely, unconfirmed — there is no token-based
  nested-group resolution at all.
- **It widens every user token** with another scope, cutting against the
  token-security posture already written into `CLAUDE.md`.

Non-deterministic authorization is worse than none, because it is
unfalsifiable in review. Resolve groups server-side.

Keep the service account's scope list minimal and read-only: group reads for
role resolution, plus Sheets write for the audit log. That list *is* the
security boundary — it belongs in version control and in change review.

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

**Decision: build the RBAC layer.** A simpler option was considered and
rejected — see §5.2 for what it was and why. The short version is that RBAC
was chosen deliberately for future-proofing: it puts permissions in the Admin
console rather than in deploy configuration, and it extends to per-tool,
per-person rules without re-architecting. §5.2 also records the interim posture
to run *while* this is being built, so onboarding is not blocked on it.

Three layers already do useful work before any of this exists, and the design
depends on understanding what each does:

| Layer | Answers | Already enforced? |
|---|---|---|
| Google's own permissions | "May this *person* do this in Google at all?" | **Yes — free.** The MCP acts as the user with the user's token, so admin APIs and other people's data already 403. |
| Workspace App access control | "May this person connect to the MCP at all?" | Available, unused. Admin-console only, no code. |
| **This phase** | "Should the *agent* do this on the user's behalf?" | No — the gap being closed. |

That third row is the whole justification. Google will happily let someone
delete their own Drive file or send their own mail, because they genuinely hold
that permission. What Google cannot distinguish is *the person deciding* from
*an agent deciding for them* — which matters because prompt injection can make
an agent act on content it merely read. OWASP's LLM Top 10 treats this as its
own risk class (*Excessive Agency*): an agent should be limited to what it
needs, not everything its identity could do. Capping destructive tools here is
a blast-radius limit for the injection case, not a restatement of Google's
permissions.

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

**Role source: Google Groups — verified feasible against the live tenant.**

Two facts checked directly rather than assumed:

- `peter.wilce@jit-logistics.com` resolves to **6 groups** (`jit-all@`,
  `jit-ops@`, `jit-compliance@`, `jit-asset-cust@`,
  `jit-externalshare-publishers@`, `ops@`). Cross-domain resolution works; a
  secondary-domain user's groups come back correctly.
- **The groups are flat.** Every member sampled across `otb-all@`,
  `otb-fin-lead@` and `otb-asset-portfolio@` is `type=USER` — no
  group-within-group nesting anywhere. This matters a great deal: transitive
  (nested) group resolution is the Enterprise-gated, 403-prone part of the
  Google APIs. OTB does not use nesting, so **only direct membership is needed
  and the edition gate never applies.**

Map groups → roles in a new `core/group_roles.yaml`. A user in several groups
receives the **union** of their roles, most-permissive-wins. Starting point
using the real groups:

| Group | Role |
|---|---|
| `otb-it-admins@otbgroup.co.uk` | `admin` |
| `otb-exec@otbgroup.co.uk` | `publisher` |
| `otb-externalshare-publishers@otbgroup.co.uk` | `publisher` |
| `jit-externalshare-publishers@jit-logistics.com` | `publisher` |
| `otb-fin-lead@`, `otb-hr-lead@`, `otb-governance@` | `contributor` |
| `jit-ops@`, `jit-compliance@`, `jit-finance@` | `contributor` |
| `otb-all@`, `jit-all@`, `vale-all@`, `bir-all@` | `reader` |
| *(no matching group)* | none — denied |

Note how well the existing structure already fits: the
`*-externalshare-publishers@` groups literally encode "allowed to share
outward", which is exactly the capability worth gating. Peter lands on
`publisher` via the union of `jit-all` → reader, `jit-ops` → contributor,
`jit-externalshare-publishers` → publisher.

The payoff is operational: **permissions are administered in the Admin
console.** Move someone into `jit-ops@` and their MCP access changes at the
next cache expiry. No redeploy, no code change, and it stays correct when staff
move between entities.

**The lookup must not run on the caller's credentials.** Not via the existing
`list_user_groups` tool, nor any other `@require_google_service` path — those
require the caller to be a Workspace admin and would 403 for every ordinary
teammate (§5.1). Group resolution belongs in `core/authz.py` using the service
account's own credential. The API call itself is unchanged
(`groups().list(userKey=…)`); only the credential differs.

**Failure mode — decide this deliberately.** What happens when the group lookup
fails (API blip, expired key, revoked role)? Neither obvious answer is
acceptable: hard fail-closed turns one Google hiccup into a total outage, and
fail-open removes the authorization layer exactly when something is wrong. So:

- cache resolved roles with a short TTL (~5–10 min);
- **serve stale on lookup failure** up to a bounded staleness (~1 hour);
- only then fail closed;
- keep a small **break-glass static allowlist** in env granting `admin` to one
  or two named addresses, so a bad service-account key cannot lock you out of
  your own tooling.

The break-glass list is not optional. Without it, the credential that gates
everything is also the credential that, when broken, prevents you fixing it.

**A second, free lever worth taking.** Workspace's own *App access control*
(Admin console → Security → Access and data control → API controls → Manage
Third-Party App Access) can pin the MCP's OAuth client to Trusted/Limited and
scope it to specific org units. That is Google-enforced, identity-based, and
delivers a coarse per-user ACL — the thing `CLAUDE.md` parks as out of scope —
without writing any code. Use it to gate *who can connect at all*, and the
role layer to gate *what they can do*.

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

### 5.2 The simpler option, and the interim posture

**What was considered.** Run one deployment per tier — a `standard` instance
with read plus safe writes, and an `admin` instance with the full toolset
including `gadmin` — each with its tool set fixed at boot via the existing
`--tools` / `--tool-tier` / `--read-only` machinery, and gate *who reaches
which* with Workspace App access control scoped to org units. No new
authorization code at all.

**Why it was rejected.** It only works while the requirement stays at two or
three coarse tiers. The moment a rule cuts across them — "ops can soft-delete
but not send as the group inbox", "this contractor gets Drive read only" — the
answer becomes another deployment, and the number of instances grows with the
number of distinct permission sets. Permissions also live in deploy
configuration rather than the Admin console, so every change is a redeploy.
RBAC was chosen for exactly this reason: one deployment, policy in config,
identity resolved per call, extensible to per-person rules.

**Keep the IdP gate anyway — the two are not alternatives.** App access control
costs nothing to enable and answers a different question ("may this person
connect at all?"). Use it to scope the OAuth client to the org units that
should have the MCP, and let RBAC decide what they can do once connected. It
also gives the only control that survives an app-layer misconfiguration.

**Interim posture — do not block onboarding on the RBAC build.** The tier-trim
above is a perfectly good stepping stone and requires no code:

1. Complete Phase 0 (the hard blockers).
2. Deploy with a **conservative fixed tool set** — no `gadmin`, no destructive
   tools — and onboard staff onto that.
3. Keep the full toolset to your own account, via a second instance or the
   existing deployment.
4. Ship RBAC, then collapse back to one deployment with policy doing the work.

This gets the team productive on a safe surface within days rather than waiting
weeks for the authorization layer. It also derisks the RBAC rollout: by the time
policy is enforcing, real usage patterns will have shown which tools each group
actually needs, so the role table is grounded in evidence rather than guesswork.

### Phase 3 — Audit integrity

13. Move audit writes to the **service account** so users no longer need
    Editor on the sheet (#5). This removes the cross-user read of other
    people's `params_summary` at the same time. **Pulled forward into §5.1** —
    once the service account exists for role resolution, this is nearly free,
    and it should not wait behind Phase 2.
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

The two migrations in §7 should come **first**, before Phase 0, because both
get materially more expensive once there is more than one user.

**Onboarding is not gated on Phase 2.** RBAC is the destination, but the interim
posture in §5.2 lets the team start on a conservative fixed tool set as soon as
Phase 0 lands. Practical order:

| When | What | Team can use the MCP? |
|---|---|---|
| Week 1 | §7 migrations, then Phase 0 blockers | not yet |
| Week 1–2 | Deploy conservative tool set + App access control | **yes, safe subset** |
| Week 2 | Phase 1 SSO hardening, service account (§5.1) | yes |
| Weeks 3–4 | Phase 2 RBAC, informed by real usage | yes, then full policy |

Building RBAC *after* people are using a safe subset is strictly better than
before: the role table gets written from observed need rather than guesswork.

### Two cheap checks to run before writing any code

Both are minutes of work and both de-risk decisions the rest of the plan
rests on:

1. **Enumerate the domains.** Admin console → Account → Domains (or
   `directory.domains.list`). Record which are secondary (host their own
   sign-in accounts) versus alias (extra addresses only). `blakefamily.uk` is
   currently unclassified. Copy the customer ID while you are there for the
   record, even though the app cannot read it at runtime.
2. **Confirm the `hd` behaviour empirically.** Have
   `peter.wilce@jit-logistics.com` complete the OAuth flow against a staging
   client and log the decoded `hd` and `email`. Expected `hd=jit-logistics.com`.
   This is the one link in the domain analysis that rests on inference rather
   than a quotable line from Google, and it gates a production allowlist.

Worth logging the claims dict once in staging too: access-token introspection
generally returns no `hd` at all, so the email-domain fallback branch
(`auth_info_middleware.py:72`) is probably the one actually running — which
means the code comment claiming `hd` is preferred "because it's IdP-attested"
overstates what is really being enforced.

---

## 7. Housing: new GCP project, new repository

### 7.1 New Google Cloud project — and do it now

Not the Workspace tenant, which stays exactly as it is (same four OUs, same
groups, same users). Only the API project and its credentials change.

Reasons:

- The current project (`849437400675`) carries **six OAuth clients all named
  "OTB Workspace MCP"** with wildly different scope sets (§3.3). One holds
  full `drive` + `spreadsheets`; another ~30 scopes. A fresh project gives one
  clean client, one clean consent screen, and a correctly-scoped service
  account for delegation.
- The old clients can then be revoked wholesale without touching the new one.
- The Internal setting (§4.1) is cleanest applied to a project created for it.

**The timing argument is the strong one, and it expires.** Migrating means
every user re-consents. Today that is one person. After onboarding it is
twenty. This is the cheapest it will ever be.

### 7.2 New repository — rename and re-home, do not rewrite

Move to a **private** `Otblakee/otb-workspace-mcp` (or preferred name), seeded
by pushing the existing history to the new remote so blame survives. Do not
start over: the ~30k lines of tool implementations are the asset.

- Keep upstream (`taylorwilsdon/google_workspace_mcp`) as a named remote for
  cherry-picking, but **stop treating it as a merge source.** Because
  `BLOCKED_TOOLS` is a denylist (#13), a routine `git pull` from upstream can
  silently register new destructive tools. Every upstream change needs
  deliberate review. The deny-by-default policy in Phase 2 fixes this
  properly.
- **Delete the distribution artifacts**: `publish-mcp-registry.yml`,
  `smithery.yaml`, `glama.json`, the DXT packaging. That workflow publishes to
  PyPI and the MCP Registry — on a repository that will hold this
  organisation's security configuration, that is a footgun with no upside.
  It is dormant today (no Actions run has ever executed in the fork), but
  dormant is not the same as removed.
- The current name `workspace-mcp-fixed` describes a fork of a fork. The
  divergence — audit logging, tool policy, soft delete, large-file support,
  multi-user security, and now authorization — has made it a different
  product.

### 7.3 Render sizing

**Standard (2 GB / 1 CPU) is right, and will carry well past 20 users.**
Session state is a few KB per user. Calls are I/O-bound waiting on Google,
which async handles well on a single core.

The real ceiling is architectural, not the plan tier: the streamable-HTTP
transport is session-stateful and `OAuth21SessionStore` is in-memory, so the
service is pinned to one worker and every session dies on redeploy. Scaling
horizontally requires moving that store to shared storage first — the OAuth
*proxy* storage already supports Valkey; the session store does not yet.

Memory is worth watching only if several users run large uploads or downloads
concurrently; the `gc.collect()` after every tool call exists because
googleapiclient leaks, which is a real signal. If OOM appears, Pro (4 GB) is
the next step — but the single-worker limit will bite before RAM does.

Also unset in production: `WORKSPACE_ATTACHMENT_DIR`, so attachments are
currently written to ephemeral container storage rather than the persistent
disk (`FOLLOWUPS.md` records this).
