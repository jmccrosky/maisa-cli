# Maisa reverse-engineering notes

## Platform
- Maisa = **Epic MyChart**, branded, on the **Apotti** platform (HUS Helsinki,
  City of Helsinki, Vantaa & Kerava).
- Host `www.maisa.fi`, app base `/maisa`. Two UI generations coexist:
  - **React SPA** features under `/maisa/app/...` calling clean `/maisa/api/...`
    JSON endpoints (messages, test results, letters, health summary, histories).
  - **Legacy ASP.NET MVC** features (Visits/appointments, CareTeam, Medications)
    at `/maisa/<Controller>/<Action>` returning JSON or server-rendered HTML.

## Authentication
1. `GET /maisa/Authentication/Login` → SPA login.
2. → `GET /maisa/Authentication/Saml/Login?idp=TUNNISTUS` (SAML to Suomi.fi).
3. **Suomi.fi e-Identification** (`tunnistautuminen.suomi.fi`) — strong auth via
   bank / mobile certificate / ID card. Requested LoA: `fLoA3;eLoA3;fLoA2;eLoA2`.
4. SAML assertion POSTed back to `/maisa/Authentication/Saml/...`; MyChart sets
   session cookies and lands on `/maisa/Home/`.

### Session cookies (httpOnly)
`_Host-MyChart_Session`, `MyChartSessionToken4maisa`, `MyChartAccessToken4maisa`,
`MyChartNetAuthenticationTicket4maisa`, `MyChartPersistence`,
`UpdateMyChartContext4maisa`, `ASPSESSIONID*`, `__RequestVerificationToken_*`,
plus F5/BIG-IP `TS*` cookies. All are **session cookies** (no hard expiry);
validity is enforced **server-side** (inactivity + absolute limits). Keepalive
pings (`/maisa/Home/KeepAlive`, `/maisa/keepalive.asp`) reset the inactivity clock.

### Why the CLI uses Playwright only for login
The session cookies are httpOnly and cannot be read by page JS; the browser-tool
sandbox additionally blocks cookie/token/base64 exfiltration. Playwright, run
locally, reads the cookies at the browser level and writes them to
`~/Library/Application Support/maisa/state.json`. After that, **all data calls
use plain httpx** — verified working (no TLS-fingerprint rejection).

### CSRF
State-changing MVC POSTs and the `/api/...` JSON POSTs require the anti-forgery
token from `GET /maisa/Home/CSRFToken` (parse the hidden input `value`), sent as
the `__RequestVerificationToken` (and `verificationToken`) header. `/api/...`
endpoints take a JSON body; MVC endpoints take form-encoded bodies.

### Persistence — two mechanisms

**A. Web session (what the CLI uses today).** The captured cookies persist on
disk and replay over httpx. Observed lifetime: the session expired ~1 h after
login (Epic inactivity + absolute timeout). `maisa keepalive` resets the
inactivity clock; past the absolute limit, re-run `maisa login`. `www.maisa.fi`
has no usable OAuth (`/maisa/oauth2/*` → 404; root `/oauth2/authorize` → 403,
edge-segmented). This is the pragmatic persistence, bounded to ~an hour.

**B. SMART-on-FHIR refresh token (the mobile FaceID mechanism) — REVERSE
ENGINEERED, implemented in `maisa/oauth.py`.** The mobile app's durable login is
Epic SMART OAuth2, confirmed from Apotti's public SMART discovery:
- FHIR base (sandbox/ecosystem): `https://gw.apottiekosysteemi.fi/Interconnect-FHIR-EKO01/api/FHIR/R4`
- authorize: `https://gw.apottiekosysteemi.fi/Interconnect-EKO01/oauth2/authorize`
- token: `.../Interconnect-EKO01/oauth2/token`
- `grant_types_supported`: `authorization_code`, **`refresh_token`**, …
- `code_challenge_methods_supported`: `S256` (PKCE)
- `capabilities`: `launch-standalone`, `client-public` (no secret),
  `permission-offline` (→ refresh token), `context-standalone-patient`

Flow = Authorization Code + PKCE, scope incl. `offline_access` → access token
(short) + **refresh token (long-lived)**. Refresh uses only `client_id` +
`refresh_token` (public client). One bank login → weeks of headless refresh —
genuinely FaceID-equivalent, and sanctioned.

**The one gate:** it needs a `client_id` registered at fhir.epic.com ("Build
Apps") and **enabled by Apotti for production patient access** (`ekosysteemi@apotti.fi`).
The EKO01 host above is the ecosystem sandbox (test patients only); production
patient data is on a PRD Interconnect host that returns 403 to unregistered
clients. The first-party Maisa app's client_id is embedded in the app and likely
attestation-bound — extracting/reusing it is neither clean nor reliable, so the
recommended route is registering your own patient-access client. `maisa/oauth.py`
implements the whole flow and works the moment `MAISA_OAUTH_CLIENT_ID` +
`MAISA_FHIR_BASE` are set (offline parts verified by `scripts/oauth_selfcheck.py`).

## Endpoint map (verified against a live session)

### Identity / shell
- `GET  /maisa/Home/CSRFToken` → anti-forgery token (HTML hidden input)
- `GET  /maisa/ProxySwitch` → self + proxy (family) accounts
- `POST /maisa/api/search/LoadMenuInfo` → full feature menu with links
- `GET  /maisa/Home/KeepAlive`, `GET /maisa/keepalive.asp` → session keepalive

### Messages  (`/maisa/api/conversations/...`, JSON POST)
- `GetFoldersList {}` → folders + unread/total counts (tags: 1 inbox, 2 archive,
  3 bookmarked, 6 automated)
- `GetConversationList {tag, localLoadParams:{loadStartInstantISO,loadEndInstantISO,pagingInfo:1}, externalLoadParams:{}, searchQuery}` → conversations
- `GetConversationDetails {id, messageId:"", organizationId:""}` → `messages[]`
  (each: `author{empKey|wprKey,displayName}`, `deliveryInstantISO`, `body` HTML,
  `attachments`), `replyFlags{canReply,cannotReplyReason}`, `users{}`
- `GetMessageMenuSettings {organizationId}` → `topicList[]` (compose subjects)
- Compose+send: `GetComposeId {menuItemId}` → composeId; `GetComposeSettings
  {composeId}` → limits (maxSubject 254, maxMessage 2000, attachments);
  **`SendReply {conversationId=composeId, organizationId, subject, messageBody, attachments:[]}`**
- `BulkConversationAction {conversationIds, action}`; `SaveReplyDraft`,
  `DeleteReplyDraft`, `DeleteExternalMessage`, `RemoveComposeId`

### Test results  (`/maisa/api/test-results/...`)
- `GetList {groupType:"UNINITIALIZED", searchString, maxResults, isCurAdmFilterEnabled}`
  → `newResultGroups[].resultList` (id strings) + `newResults{id[^]:detail}`
- `GetDetailsByCSN {organizationID, csn}` → full result detail

### Letters  (`/maisa/api/letters/...`)
- `GetLettersList {}` → `letters[]` (`hnoId, csn, reason, dateISO, viewed, empId`) + `users{}`
- `GetLetterDetails {hnoId, csn}` → `bodyHTML`

### Health record
- `POST /maisa/api/health-summary/FetchHealthSummary {}` → header (age/height/weight), conditions, journeys
- `POST /maisa/api/allergies/LoadAllergies {isHealthSummary:true}`
- `POST /maisa/api/HealthIssues/LoadHealthIssuesData {isHealthSummary:true}`
- `POST /maisa/api/immunizations/LoadImmunizations {}`
- `POST /maisa/api/medications/LoadMedicationsPage {context:2}`
- `POST /maisa/Clinical/CareTeam/Load` (MVC, form+CSRF) → providers

### Appointments / visits  (`/maisa/Visits/VisitsList/...`, MVC form+CSRF)
- `LoadUpcoming` → `InProgressVisits`, `NextNDaysVisits`, `LaterVisitsList`
  (each: `VisitTypeName, Date, Time, PrimaryProviderName, PrimaryDepartment{Name,Address,PhoneNumber,Instructions}, CsnForECheckIn`)
- `LoadPast {serializedIndex}`, `LoadAppointmentRequest`, `VisitsHeaderOptions`

## ID encoding
Epic record IDs are URL-safe base64 with custom escaping (`$`→`-24`, `=`→`-3D`,
`+`→`-2B`, `/`→`-2F`). Pass them back **verbatim**; never decode/modify.
