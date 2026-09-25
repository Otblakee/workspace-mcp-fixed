# Gmail signatures: operator runbook

Steps, in order, to bring centrally managed Gmail signatures live on the OTB
Workspace tenant. Read `TEMPLATES.md` for the template brief and `CLAUDE.md`
(section "Gmail signatures") for the design. Nothing below needs a code
change; every step is Google Cloud, Admin console, Render or a tool call.

Plain rule for the whole feature: the service account can act as any user in
the tenant, so treat its key like the tenant's master password. It lives in
one Render secret file and nowhere else.

## 1. Create the service account

In the existing OTB GCP project (the one that already holds the MCP's OAuth
client; do not create a new project):

1. IAM and admin > Service accounts > Create service account.
2. Name it `workspace-signatures`. Description: "Gmail signature management
   for workspace-mcp (domain-wide delegation)".
3. Grant it **no IAM roles**. Domain-wide delegation does not need any, and a
   role would only widen what a leaked key could do inside GCP.
4. Finish. Open the account and note two values:
   * its email address (`workspace-signatures@<project>.iam.gserviceaccount.com`),
     which the ledger Sheet is shared with in step 3;
   * its numeric **Unique ID** (the "OAuth 2 Client ID" on the details page),
     which the Admin console needs in step 2.
5. Keys tab > Add key > Create new key > JSON. Download it once. This file is
   the secret in step 4. Do not commit it, email it, or paste it into a chat.

## 2. Grant domain-wide delegation

Admin console path:

**Security > Access and data control > API controls > Manage Domain Wide
Delegation > Add new**

* Client ID: the service account's numeric Unique ID from step 1.
* OAuth scopes: exactly these three, comma-separated, nothing else:

```
https://www.googleapis.com/auth/gmail.settings.basic,https://www.googleapis.com/auth/admin.directory.user.readonly,https://www.googleapis.com/auth/admin.directory.group.member.readonly
```

They are `gsignatures.sa_auth.DELEGATED_SCOPES`, in that order. The list in
code and the list in the console must match; a test pins the code list.

What each one is for:

| Scope | Used for | Impersonating |
| --- | --- | --- |
| `gmail.settings.basic` | `users.settings.sendAs.list/get/patch` (the signature only) | each user |
| `admin.directory.user.readonly` | `users.get` / `users.list` for name, title, mobile, OU | the Directory admin (`SIGNATURE_DIRECTORY_ADMIN`) |
| `admin.directory.group.member.readonly` | `members.list` for group scopes | the Directory admin |

**The Gmail sharing settings scope (`settings.sharing`) is deliberately NOT granted.** Gmail's API puts
send-as updates under two scopes. The basic one is enough to change a
signature. The sharing one also covers forwarding, delegation (mailbox
access for another person) and creating or deleting send-as addresses. A key
that only holds the basic scope cannot be used to forward anyone's mail or
hand their mailbox to someone else, even if it leaks. Never add the sharing
scope to this entry.

Delegation can take a few minutes to propagate. If the first tool call
returns `unauthorized_client`, wait ten minutes and retry before changing
anything.

## 3. Create the ledger Sheet

1. Create a Google Sheet named **OTB_LOG_SignatureLedger_2026-09-25_v1** in
   the IT records folder (the same area as the MCP audit log). Leave it
   empty; the `Ledger` tab and its header are created on first write.
2. Share it as **Editor** with the service account's email address from
   step 1. The ledger is written as the service account itself, not as any
   user, so this share is the only way it can write.
3. Note the Sheet ID from the URL for step 4.

The ledger is append-only by convention. Every live apply adds one row per
send-as address with the hash Gmail returned after the write and the
previous signature HTML. Audits compare Gmail against these rows. Do not edit
or delete rows; if something is wrong, apply again (the new row wins).

## 4. Render environment group `signatures`

Create an environment group named `signatures` and attach it to both the web
service and the cron job (step 8).

Secret file:

* `signature-sa.json`: the JSON key from step 1. Render mounts it at
  `/etc/secrets/signature-sa.json`.

Environment variables:

| Variable | Value |
| --- | --- |
| `SIGNATURE_SERVICE_ACCOUNT_FILE` | `/etc/secrets/signature-sa.json` |
| `SIGNATURE_DIRECTORY_ADMIN` | `oliver@otbgroup.co.uk` (the admin impersonated for Directory reads) |
| `SIGNATURE_ADMIN_EMAILS` | `oliver@otbgroup.co.uk` (comma-separated allowlist of who may call the tools) |
| `SIGNATURE_LEDGER_SHEET_ID` | the Sheet ID from step 3 |

The web service prints none of these at start-up, by design. Check them in
the Render dashboard, not the logs.

## 5. Enable the service

Add `gsignatures` to the `TOOLS` variable on the web service:

```
TOOLS=gmail drive calendar docs sheets contacts gsignatures
```

`gsignatures` is an opt-in service (`OPT_IN_TOOLS` in `main.py`): it never
loads unless `TOOLS` names it, whatever the tier. Its five tools sit at the
core tier so `TOOL_TIER=extended` picks them up unchanged. Redeploy.

Confirm in the start-up banner that the service loaded, and that the OAuth
consent prompt is unchanged: the feature requests no OAuth scope of its own.

## 6. Pilot on the owner

Do these in order, from a connected client signed in as an address on
`SIGNATURE_ADMIN_EMAILS`. Any other caller is refused before any Google call.

1. `preview_email_signature(user_email="oliver@otbgroup.co.uk")`. Check the
   entity, versions, name, title, mobile and the HTML. Expect the
   `statutory_verified: false` warning until step 6 of `TEMPLATES.md` is done.
2. `get_email_signatures(user_email="oliver@otbgroup.co.uk")`. Every send-as
   should show as `never_applied` (managed) or `unmanaged` (the
   `blakefamily.uk` alias).
3. `set_email_signature(user_email="oliver@otbgroup.co.uk")`: the default dry
   run. Read the table; the action should be `would_apply`.
4. `set_email_signature(user_email="oliver@otbgroup.co.uk", dry_run=False,
   confirm=True)`: the primary, live. Then the same with
   `send_as_email="oliver@bir-d.co.uk"` (one alias). Keep both result tables.
5. Open the ledger Sheet: two rows, `readback_hash` filled, the old
   signature in `previous_signature_html`.
6. Check the signature in Gmail on the web (Settings > See all settings >
   General > Signature, and compose a message) and in the Gmail app on a
   phone (compose; if a plain-text mobile signature is set in the app it
   wins, and that is expected).
7. Compare what Gmail kept with what was sent: `get_email_signatures` shows
   the current hash; the HTML is visible in Gmail's signature editor source
   or via a test message. Record in `TEMPLATES.md` ("Layout rules") what the
   sanitiser stripped or rewrote, so the branded templates are written to
   survive it.
8. `audit_email_signatures(ou_path="/01 OTB")`: the owner's rows should now
   read `in_sync`.

## 7. Rollout by OU

For each OU (`/01 OTB`, `/02 JIT`, `/03 VALE`, `/04 BIR`, then the AHWE
domain rule via `domain="arthistorywithemily.co.uk"`):

1. `apply_email_signatures(ou_path="/02 JIT")`: dry run. Read every row.
   `error` rows usually mean a missing job title in the Directory; fix the
   Directory and re-run the dry run until the table is clean.
2. `apply_email_signatures(ou_path="/02 JIT", dry_run=False, confirm=True)`.
3. Keep the result table and the JSONL report link with the change record
   for that OU. The ledger holds the rows; the table is the human-readable
   evidence of the run.
4. `audit_email_signatures(ou_path="/02 JIT")` the next day to confirm
   nothing moved.

`max_users` (default 200) refuses a scope larger than that with the count.
Raise it deliberately for a large OU; never as a reflex.

## 8. Render cron job (weekly audit)

Create a cron job in the same Render workspace:

* Repository and branch: the same as the web service; runtime Docker with
  the same `Dockerfile`.
* Command: `uv run python -m gsignatures.audit_cli --all`
* Environment group: `signatures` (step 4). The cron needs no `TOOLS`,
  `TOOL_TIER` or OAuth variables; it never starts the MCP server.
* Schedule: Monday 07:00 UK. Render cron schedules are in UTC, so use
  `0 6 * * 1` while the UK is on BST (late March to late October) and
  `0 7 * * 1` while it is on GMT. If nobody will maintain the switch, leave
  `0 7 * * 1` all year and accept 08:00 in summer.

The cron audits only. It never re-applies a signature. Its exit code is the
signal: `0` when every managed address matches the ledger, `2` when any row
is `never_applied`, `stale_template`, `changed_since_apply` or `error`, `1`
on a fatal error (config, key, ledger). Render marks the run failed on a
non-zero exit and emails the workspace, so an email from the cron means
drift or a broken setup, and a human decides what to do. The run also writes
an `Audit_<UTC date>` tab to the ledger Sheet (skip with `--no-report`).

## 9. Rollback

Fastest to slowest:

1. **Stop the tools:** remove `gsignatures` from `TOOLS` and redeploy. The
   five tools disappear from every client. Nothing else changes.
2. **Kill the capability:** in the Admin console, delete the domain-wide
   delegation entry from step 2. The key can then do nothing in the tenant,
   whether or not it has leaked. Do this first if a leak is suspected, then
   rotate the key (step 10).
3. **Restore a signature:** the ledger row for the address holds
   `previous_signature_html`. Paste it back in Gmail's signature editor (or
   apply it via the API by hand). Setting `force=True` after reverting the
   template in git is the managed way for many addresses.
4. **Revert content:** revert the template files and the pinned versions in
   `config/entities.yaml` in git, redeploy, and run `apply_email_signatures`
   again (dry run, then live). The ledger records the new versions, so the
   audit stays honest.

## 10. Key rotation

1. Create a new JSON key on the service account (step 1.5). The Unique ID
   and the delegation entry do not change.
2. Replace the secret file `signature-sa.json` in the `signatures`
   environment group. Redeploy the web service (the cron picks the file up
   on its next run; credentials are built fresh per call, never cached).
3. Run `preview_email_signature` once to confirm the new key works.
4. Delete the old key in Google Cloud.

Rotate on a schedule (quarterly is reasonable) and immediately on any
suspicion of exposure.

## Known limitation: Gmail clients only

The signature set through the API is Gmail's own signature. It is used by
Gmail on the web and by the Gmail mobile apps (unless the app has its own
plain-text mobile signature set). Apple Mail, Outlook and any client that
reaches Gmail over IMAP or a connector keep their own local signatures and
never see this one. That is not solved by this feature; it needs a client
side policy or a different product.
