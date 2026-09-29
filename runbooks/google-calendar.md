# Google Calendar reminder projection

q-core is authoritative for reminders. Google Calendar is a one-way delivery
projection used for phone notifications; it is not a second reminder database.
Creating a q-core reminder creates or updates an event in a dedicated
`q-core Reminders` calendar. A full sync overwrites edits made to projected
events and recreates an event that was deleted in Google. Deleting the q-core
reminder removes its mapped Google event.

The integration requests only
`https://www.googleapis.com/auth/calendar.app.created`, which permits access to
calendars created by this application. It does not request access to the
user's other calendars.

## Google Cloud setup

1. Create or select the q-core project in Google Cloud.
2. Enable **Google Calendar API**.
3. Configure Google Auth Platform branding and audience. For a personal
   external app in testing, add the Google account that will own the calendar
   as a test user.
4. Create an OAuth client with application type **Web application**.
5. Add the exact authorized redirect URI for each instance that will sign in:

   ```text
   https://<serve hostname>/integrations/google/callback   # the server, through Tailscale Serve
   http://localhost:8420/integrations/google/callback      # a Mac/launchd instance
   ```

   The API picks one automatically. With `Q_CORE_SERVE_HOSTNAME` set (the server),
   it uses the Serve URL, so **any browser on the tailnet** can finish the
   sign-in: phone, Mac or a Linux laptop. The callback is in the Serve browser
   surface for the allowlisted Tailscale identity, and it's still bound to its
   single-use `state`. Without a Serve hostname it uses `localhost` (not
   `127.0.0.1`) on `Q_CORE_PORT`. The registered URI must match exactly.

   If Google ever refuses the `ts.net` redirect, sign in through an SSH forward
   from any machine instead (`ssh -L 8420:127.0.0.1:8420 <server>`) and register
   the localhost URI. The callback on the service port needs no credential; it's
   authenticated by its `state`.

No JavaScript origin is required: the browser visits Google's authorization
page and Google redirects to the server-side callback.

## Store the client credentials

**On the server (NixOS)** the client id and secret come from sops, as
`Q_CORE_GOOGLE_OAUTH_CLIENT_ID` and `Q_CORE_GOOGLE_OAUTH_CLIENT_SECRET` in
the service's environment file (configured on the host). There's no
Keychain on Linux, and the file store never falls back to one.

**On a Mac**, Keychain is preferred over `.env`. Replace the placeholders
locally; don't paste the secret into a ticket, transcript or committed file.

```bash
security add-generic-password -U \
  -s q-core.google-oauth-client -a client-id -w '<oauth-client-id>'
security add-generic-password -U \
  -s q-core.google-oauth-client -a client-secret -w '<oauth-client-secret>'
```

Alternatively, the API accepts `Q_CORE_GOOGLE_OAUTH_CLIENT_ID` and
`Q_CORE_GOOGLE_OAUTH_CLIENT_SECRET` from `.env`.

**Where the refresh token lives** (`Q_CORE_GOOGLE_TOKEN_STORE`; by default
`keychain` on macOS and `file` elsewhere):
- **keychain:** service `q-core.google-calendar`, account `refresh-token`.
- **file:** `Q_CORE_SECRETS_DIR/google-refresh-token`, a 0600 file in a 0700
  directory owned by the service user, replaced atomically. On the server that's
  `/srv/q-core/data/secrets/`. It's included in the host's backups: ZFS
  snapshots, and restic to off-site storage, which is client-side encrypted (The owner's decision
  2026-09-25). A restore therefore brings the connection back without a new
  sign-in.

The token is never written to the database or logs. The database stores only
the created calendar id, sync health and reminder-to-event mappings. The
connection row's `keychain_service` column is historically named: it records
the token store, either the Keychain service (`q-core.google-calendar`) or
`file`. It was kept rather than renamed, because a rename would be a migration
for a label.

**One syncing instance at a time.** Each instance that signs in gets its own
refresh token, and Google allows that. But two instances projecting reminders
into the same calendar would create duplicate events. After cutover only
the server's q-core syncs. A legacy instance on a Mac keeps its token as the dormant
fallback, with calendar sync off (don't run `sync_google_calendar` there). The
fallback drill turns it back on only while the server's instance is stopped.

Restart the launchd server after changing credentials:

```bash
launchctl kickstart -k gui/$(id -u)/tech.q-core.api
```

## Connect and verify

1. Call `connect_google_calendar` through q-core MCP.
2. Open the returned `authorization_url` in a browser and approve access.
3. Google redirects to the callback (the Serve URL on the server, localhost on a
   Mac), which stores the refresh token and creates `q-core Reminders`
   automatically.
4. Call `google_calendar_status`; it should report `connected: true`.
5. Call `sync_google_calendar` once to project reminders that predate the
   connection.

New reminders sync immediately after their local row commits. Provider errors
do not roll back the local reminder; status records a non-sensitive error and
an explicit `sync_google_calendar` retries all reminders.

Timed reminders are one hour long and use `Q_CORE_TIMEZONE` (default
`UTC`). Date-only reminders become all-day events. The default
notification offsets are 1,440 and 60 minutes (one day and one hour); pass
`notification_offsets_minutes` when creating a reminder to override them.

## Recovery and diagnostics

- `google_calendar_status` is the first check. A stored connection means the
  integration is configured; `last_error` means at least one automatic sync
  needs retrying.
- If an event was changed or deleted in Google, run `sync_google_calendar`.
  q-core will overwrite it or recreate it under the dedicated calendar.
- If credentials were rotated, replace the two Keychain entries (on the server,
  the two sops values plus a rebuild), restart the server, and reconnect if
  Google has also revoked the refresh token.
- If the refresh token was revoked, remove only it and repeat the connection
  flow. On the server, delete `/srv/q-core/data/secrets/google-refresh-token`. On
  a Mac, delete the Keychain item:

  ```bash
  security delete-generic-password \
    -s q-core.google-calendar -a refresh-token
  ```

- Do not delete or edit Calendar mapping rows directly. The API owns the
  database, and a full sync is the supported repair path.
- API failures before application logging starts appear in
  `data/logs/api.launchd.err`; normal request/sync diagnostics are in
  `data/logs/api.log`. Neither log should contain OAuth tokens or request
  bodies.

Completion and snooze state belongs to q-core and is surfaced by
`list_due_items`; the Calendar event is a notification projection, not the
source of that state. Two-way synchronization is deliberately out of scope.

## Reminder lifecycle

Completion removes a one-off event. For a recurring reminder, completed or
snoozed dates are excluded from the master series; each snoozed occurrence
gets a mapped one-off event at its new date. Later occurrences remain intact.
Full sync rebuilds this projection from reminder definitions and instance state;
it never reads entity or relationship dates. Editing a reminder updates the same
mapping. Changing its schedule requires explicit history reset if occurrences
have been completed or snoozed. Deletions retain provider cleanup receipts until
Google accepts them, including when the last local reminder has been deleted.

Provider insert IDs are saved before sending to make timeout retries idempotent.
The single API process serializes reminder writes and projection calls; run one
API process, as the supplied launchd configuration does. Provider calls may take
up to the existing 20-second request timeout. Automatic failures leave local
changes committed and `last_error` visible; run `sync_google_calendar` to retry.
A successful full sync clears that error. There is no background retry worker.

Person and pet birthday saves maintain one reminder per entity, starting at the
next birthday (including today), at 09:00 America/New_York with offsets 10080,
1440 and 60 minutes. February 29 is observed on February 28 in non-leap years.
The birth year remains entity data. Time, notes, location and notification
settings can be overridden with `update_reminder`; entity edits preserve them.
The title, entity link and annual schedule are source-owned. Removing the date,
deactivating the entity or setting `birthday_reminder_enabled=false` removes the
generated reminder and queues Calendar cleanup. Changing the birthday resets
its old completion/snooze history. Opting in again creates a new reminder with
default settings. Manual reminders are never silently adopted or deleted: a
linked annual reminder occurring on the next birthday (or the first birthday
on/after a later manual start) blocks automatic creation pending inspection.
This is a bounded collision guard, not exhaustive overlap detection for arbitrary
multi-year RRULEs; inspect existing manual reminders during backfill.

Existing birthdays are not backfilled at startup. Preview the people/pets and
their existing reminders with public tools, then save each selected entity's
unchanged attributes through `update_entity`. Repeating that operation preserves
the generated reminder ID. Do not claim an estimated birthday was verified by
this operation. No financial or insurance deadline automation is included.

Google recurrence semantics follow the official [Calendar event model](https://developers.google.com/workspace/calendar/api/concepts/events-calendars).
