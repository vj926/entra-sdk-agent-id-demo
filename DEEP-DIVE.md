# Deep Dive: How "Publish Agents using Entra SDK" Works End-to-End

This document explains the demo from the ground up: what was created, how the pieces connect, and what happens **in each click** when you exercise the valid Agent flow (Steps 1 → 4). The foreign-Agent negative test (Step 5) is intentionally **not** covered here — it's documented separately.

---

## Part 1 — Overview: what was created

Three logical "things" were brought into existence to make this demo work. Two live in **Microsoft Entra** (your identity provider) and one is the **runtime** that uses them.

### 1. Blueprint app registration (in Entra)
- A standard Entra app registration. App ID: `a90a55dc-5702-46a4-9dc0-e263cc37e909`.
- It owns a **client secret**. This is the *only* credential in the entire system.
- Its sole job is to be the "credential holder" — to authenticate to Entra so that, on its behalf, Entra can issue tokens to its child Agent Identities.
- It has **no permissions** of its own that the demo cares about. It does *not* have `User.Read.All`. It can't actually call Graph as itself for any real work.
- A service principal of this app exists in the tenant (Object ID: `8b160754-4b61-4ecb-826b-c481917b9fa7`).

### 2. Agent Identity app registration (in Entra)
- Another Entra app registration, but a special kind. App ID: `beab1093-dd04-4821-b7a2-6e692a130487`.
- It is **parented by the Blueprint** (this is the unique part). Entra stores this parent → child relationship as a **federated identity credential** on the Blueprint, with a subject like `/eid1/c/pub/t/<tenant>/a/<blueprint-handle>/<agent-app-id>`.
- It has **no client secret, no certificate, no managed identity, no credentials of any kind**. By design.
- It does have **permissions**: `User.Read.All` on Microsoft Graph (app-only), admin-consented.
- This is the identity that *actually appears* in tokens used for work, and the identity that *actually appears* in the audit trail in Entra Sign-in logs ("Is Agent: Yes / Agent type: Agent Identity / Agent parent ID: <Blueprint>").

### 3. The runtime — one Container App with two containers
Hosted in Azure Container Apps (`agent-id-demo-arcsmd` in resource group `vij_Nvidiademo_EntraSDK`):

- **Container `agent` (port 8000)** — the FastAPI demo app (this repo's `app.py`). Serves the HTML UI and exposes `/api/token`, `/api/graph-users`, `/api/info`. **Has no credentials, no client ID, no secret.** It only knows three pieces of *configuration*: the Tenant ID, the Agent's App ID, and the URL of its sibling sidecar (`http://localhost:5000`).
- **Container `sidecar` (port 5000)** — `mcr.microsoft.com/entra-sdk/auth-sidecar:1.0.0-azurelinux3.0-distroless`. This is the only piece of code in the system that holds the Blueprint's client secret. It is **private to the pod** — port 5000 is not exposed to the internet, only `localhost` inside the pod can reach it.

```
                                    ┌──────────────────────── ACA Pod ────────────────────────┐
                                    │                                                         │
   user's browser ─── HTTPS ───►  ┌─┴──── agent (FastAPI :8000) ────┐                         │
                                    │  HTML + /api/* endpoints        │ ── localhost ──► ┌──── sidecar :5000 ────┐
                                    │  knows: Tenant, AgentAppID,     │                  │ holds Blueprint secret │
                                    │         BlueprintAppID, sidecar │                  │ speaks MSAL → Entra    │
                                    └─────────────────────────────────┘                  └────────────┬───────────┘
                                                                                                       │ HTTPS
                                                                                                       ▼
                                                                                          login.microsoftonline.com
                                                                                              graph.microsoft.com
```

Two ACA secrets live in the platform (not in code):
- `blueprint-secret` — the Blueprint's client secret value, mounted into the sidecar at `AzureAd__ClientCredentials__0__ClientSecret`.

### Why this shape?
The whole point is **separation of credential and identity**:
- The credential (Blueprint's secret) is concentrated in one place (the sidecar) and never leaves the pod.
- The agent code is **identity-aware** but **credential-free** — it just names the Agent it wants a token for.
- Entra uses the parent's credential to authenticate, but issues a token whose `appid` is the **child** Agent and whose roles/permissions are the **child**'s. The audit row in Sign-in logs records both.

---

## Part 2 — Deep dive into each step (valid Agent only)

For each step, you'll see: **the actor**, **the trigger**, **the API call** with its parameters, **what the receiving side does**, **the response**, and **why** that hop exists.

### Step 1 — "Get Agent Token"

**Goal:** Mint a JWT for the Agent Identity, without the agent code ever holding any credential.

#### Hop 1: Browser → Agent container

| | |
|---|---|
| **Actor** | User clicks the **"Get Agent Token"** button in the HTML page |
| **Trigger** | Browser JavaScript: `fetch("/api/token?asAgent=true&scenario=valid")` |
| **API call** | `GET https://<demo-fqdn>/api/token?asAgent=true&scenario=valid` |
| **Receiver action** | The FastAPI handler `api_token` in `app.py` runs. It generates a fresh per-click GUID for `CorrelationId`, then calls `fetch_token(as_agent=True, ..., agent_id=AGENT_APP_ID)`. |
| **Why this hop?** | The browser must not have direct access to the sidecar. The sidecar is internal infrastructure, on `localhost:5000`, holding a secret. Only the agent code (running next to the sidecar, in the same pod) is allowed to talk to it. The browser has only one publicly reachable surface: the agent's `/api/*` endpoints on port 8000. |

#### Hop 2: Agent container → Sidecar

| | |
|---|---|
| **Actor** | The FastAPI handler in `app.py` |
| **API call** | `GET http://localhost:5000/AuthorizationHeaderUnauthenticated/graph?AgentIdentity=beab1093-dd04-4821-b7a2-6e692a130487&optionsOverride.AcquireTokenOptions.CorrelationId=<GUID>&optionsOverride.AcquireTokenOptions.ForceRefresh=true` |
| **Receiver action** | The auth-sidecar receives this. It looks up the configured downstream API named `graph` (configured at startup with scope `https://graph.microsoft.com/.default`). It now needs to acquire a token for that scope. Because the request says `AgentIdentity=<Agent App ID>`, the sidecar will ask Entra for a token *as that Agent*, not as the Blueprint. |
| **Path components** | `AuthorizationHeader` = "give me a `Bearer …` header"; `Unauthenticated` = "I, the caller, don't need to present a token to you" — the sidecar trusts in-pod localhost calls; `/graph` = the named downstream API. |
| **Key parameter — `AgentIdentity`** | This is **the lever**. Without it, the sidecar would mint a Blueprint token (Step 4). With it, the sidecar is asking Entra to mint a token *for* this Agent. |
| **Key parameter — `CorrelationId`** | A demo per-click GUID, so every click is traceable. (Note: the current sidecar version doesn't actually forward this to MSAL/Entra — known limitation. Use the JWT's `uti` claim instead to find the row in Sign-in logs.) |
| **Key parameter — `ForceRefresh`** | Bypass the sidecar's MSAL token cache, so each click triggers a real Entra request. Important because the cache key is `(client_id, scope, tenant)` — `AgentIdentity` is *not* in the key, so without `ForceRefresh` you'd see stale tokens. |
| **Why this hop?** | Off-load all OAuth + MSAL + cache + refresh logic from the agent. The agent doesn't need to know *how* a token is acquired; it only needs to name the API and the identity. |

#### Hop 3: Sidecar → Entra (the OAuth call)

| | |
|---|---|
| **Actor** | The sidecar's MSAL client (running as the Blueprint) |
| **API call** | `POST https://login.microsoftonline.com/98430660-2a7e-4e6b-b49c-800a8ba8b657/oauth2/v2.0/token` |
| **Body (form-encoded)** | `client_id=a90a55dc-…` (Blueprint App ID)<br>`client_secret=b0G8Q~…` (Blueprint client secret, from the ACA secret)<br>`scope=https://graph.microsoft.com/.default`<br>`grant_type=client_credentials`<br>plus an internal extension carrying the `AgentIdentity` (the requested child Agent) |
| **Receiver action (Entra)** | Entra performs three checks in sequence:<br>① **Authenticate**: validate the Blueprint's client secret matches the Blueprint's stored credentials. ✅<br>② **Parentage check**: confirm that the requested Agent Identity (`beab1093-…`) is registered as a child of the Blueprint via a federated identity credential. ✅<br>③ **Permission check**: confirm the *Agent* has been granted the requested scope (`User.Read.All` on Graph, app-only). ✅<br>Then mints a JWT whose claims describe the **Agent**, not the Blueprint. |
| **Why this hop?** | This is where the credential leaves the pod. Everything else is local. This is also where the security boundary is enforced — Entra is the trust authority, not the sidecar, not the agent. |
| **Audit side-effect** | Entra writes a row to **Sign-in logs → Service principal sign-ins** with: *Application = the Agent's name*, *Is Agent = Yes*, *Agent type = Agent Identity*, *Agent parent ID = Blueprint*, *Client credential type = ClientSecret*, *Status = Success*, *Unique token identifier (uti)*. This row is the demo's audit-trail proof. |

#### Hop 4: Entra → Sidecar

| | |
|---|---|
| **API response** | `200 OK` with JSON: `{"access_token":"eyJ…", "token_type":"Bearer", "expires_in":3600, …}` |
| **JWT body — claims that matter** | `appid = beab1093-…` → the **Agent**'s App ID. This is what makes Graph treat the call as the Agent.<br>`xms_par_app_azp = a90a55dc-…` → the **parent app's azp** — the Blueprint that authenticated. Proves provenance.<br>`roles = ["User.Read.All"]` → the **Agent's** permissions, not the Blueprint's.<br>`idtyp = "app"` → app-only token (no user).<br>`aud = "https://graph.microsoft.com"` → audience.<br>`tid = 98430660-…` → tenant.<br>`uti` → unique token identifier — matches the "Unique token identifier" field in the Sign-in log row.<br>`iat`, `exp` → issue and expiry times. |
| **Sidecar action** | Stores the token in its in-memory MSAL cache (keyed on client_id+scope+tenant), then formats a response for the agent. |

#### Hop 5: Sidecar → Agent container

| | |
|---|---|
| **API response** | `200 OK` with JSON: `{"authorizationHeader":"Bearer eyJ…", "expiresOn":"…"}` |
| **Why this shape?** | Convenience. The agent can copy this string straight into an outgoing request header without parsing JWTs or knowing OAuth. |

#### Hop 6: Agent container → Browser

| | |
|---|---|
| **Agent action** | `app.py` strips the `Bearer ` prefix, base64-decodes the JWT body (no signature verification needed — Graph will verify it for real on Step 3), pulls out the interesting claims, and returns them. |
| **API response** | `200 OK` JSON to the browser: `{ asAgent, agentIdUsed, bearer, bearer_short, claims, appid_is_agent, appid_is_blueprint, uti, appid, iat, app_displayname, correlationId, exp_human }` |
| **Browser action** | The JS shows a green checkmark, the truncated bearer, and the "Cross-check this token request in Entra Sign-in logs" panel with copy buttons for `Request ID`, `uti`, `appid`, `iat`. |

### Step 2 — "Decode the token"

**Goal:** Inspect the JWT we just received and confirm what it actually says.

| | |
|---|---|
| **Actor** | Browser only — pure client-side JavaScript |
| **Trigger** | User clicks **"Decode last token"** |
| **API calls** | **None.** No network traffic. The page already received the decoded claims as part of Step 1's JSON response. |
| **Action** | The JS reads `lastToken.claims` and renders a table with one row per important claim, each with ✓/✗ commentary: `aud` should be Graph's URL, `iss` should be the tenant's issuer, `appid` should equal the Agent App ID (✓) **not** the Blueprint App ID (✗), `xms_par_app_azp` should equal the Blueprint App ID (proof of provenance), `roles` should contain `User.Read.All`, `idtyp` should be `app`. |
| **Why this step?** | Without this step, the demo would be "I asked for a token and got one." That's not impressive. Step 2 is what makes the audience *see* that the token's identity is the Agent and the parent is the Blueprint — that's where Agent Identity becomes tangible. |

### Step 3 — "Call Microsoft Graph as the Agent"

**Goal:** Use the Agent token to do real work — and prove that Microsoft Graph independently accepts it as the Agent.

#### Hop 1: Browser → Agent container

| | |
|---|---|
| **Trigger** | User clicks **"Call Graph /users (as Agent)"** |
| **API call** | `GET https://<demo-fqdn>/api/graph-users?asAgent=true&scenario=valid` |
| **Receiver action** | `api_graph_users` in `app.py` runs. Generates a per-click GUID. |

#### Hop 2: Agent container → Sidecar

| | |
|---|---|
| **API call** | `POST http://localhost:5000/DownstreamApiUnauthenticated/graph?AgentIdentity=beab1093-…&optionsOverride.RelativePath=users%3F%24top%3D3%26%24select%3DdisplayName%2CuserPrincipalName%2Cid&optionsOverride.HttpMethod=Get&optionsOverride.AcquireTokenOptions.CorrelationId=<GUID>&optionsOverride.AcquireTokenOptions.ForceRefresh=true` |
| **Receiver action** | The sidecar does **two things in one call**:<br>① **Acquire a token** (same as Step 1, hop 3 — ask Entra for a token as the Agent).<br>② **Call Graph** with that token attached and the path/method specified by `RelativePath`+`HttpMethod`. |
| **Path components** | `DownstreamApi` (vs `AuthorizationHeader`) tells the sidecar to actually invoke the API, not just hand back a header. |
| **Why merge into one call?** | So the agent code never has to handle the token at all. The agent just says "GET /users?$top=3 on the graph API as this Agent" — the sidecar does both the auth dance and the HTTP call. |

#### Hop 3: Sidecar → Entra

Same as Step 1, hop 3. Result: a JWT for the Agent. (May be cache hit if `ForceRefresh` wasn't set, but the demo always passes `ForceRefresh=true`.)

#### Hop 4: Sidecar → Microsoft Graph

| | |
|---|---|
| **API call** | `GET https://graph.microsoft.com/v1.0/users?$top=3&$select=displayName,userPrincipalName,id` |
| **Headers** | `Authorization: Bearer <Agent JWT>` |
| **Receiver action (Graph)** | Graph performs an **independent** validation of the JWT. The sidecar's say-so is irrelevant here. Graph checks:<br>① **Signature** — verifies the JWT was signed by Entra's signing key (fetched from the well-known `jwks_uri`).<br>② **Issuer** — `iss` must be `https://sts.windows.net/<tenant>/` (or `…/v2.0`).<br>③ **Audience** — `aud` must be `https://graph.microsoft.com`.<br>④ **Expiry** — `exp` must be in the future, `nbf` must be in the past.<br>⑤ **Authorization** — does the `appid` in the token (the Agent) have `User.Read.All`? Yes, it does (granted in the Phase 1 setup script).<br>If everything passes → returns the data. If anything fails → returns 401/403.<br>**Important:** Graph reads `appid` from the token. That's why "the call is as the Agent" — Graph is identifying the caller by `appid`, which is `beab1093-…`. |
| **API response** | `200 OK` with JSON: `{"value": [{"displayName":"Conf Room Adams", …}, …]}` (3 users from the M365 directory). |
| **Why this hop?** | This is the *actual work* the Agent is doing. Reading users out of a directory. Steps 1-2 prove "we can mint a token"; Step 3 proves "real downstream APIs accept that token and recognize it as the Agent." |

#### Hop 5: Sidecar → Agent container

| | |
|---|---|
| **API response** | `200 OK` with JSON wrapper: `{"statusCode":200, "content":"<the Graph JSON, as a string>"}` |
| **Why wrap?** | The sidecar is a generic proxy — it doesn't know how to merge headers from arbitrary downstream APIs into its own response, so it tells the caller exactly what Graph said. |

#### Hop 6: Agent container → Browser

| | |
|---|---|
| **Agent action** | Parses the wrapper, also fetches the JWT it used (so the UI can show the same `uti`, `iat`, `appid` for cross-checking against Sign-in logs), and returns a flat JSON. |
| **API response** | `200 OK` with `{ sidecarHttp, graphHttp, graphBody, asAgent, agentIdUsed, correlationId, tokenUti, tokenAppid, tokenIat, tokenAppDisplayName, note }` |
| **Browser action** | The JS shows ✅ + a bulleted list of users + the "Cross-check in Entra Sign-in logs" panel with `uti` to find the row. |

### Step 4 — "Negative comparison: get a Blueprint token (no AgentIdentity)"

**Goal:** Prove that the *only* thing flipping the token's identity from Blueprint → Agent is the `AgentIdentity` parameter. Same code, same secret, same sidecar — just one query parameter different.

The flow is identical to Step 1, with one change:

| Hop | Step 1 (valid Agent) | Step 4 (Blueprint) |
|---|---|---|
| Browser → Agent | `GET /api/token?asAgent=true` | `GET /api/token?asAgent=false` |
| Agent → Sidecar | `…/AuthorizationHeaderUnauthenticated/graph?AgentIdentity=beab1093-…&…` | `…/AuthorizationHeaderUnauthenticated/graph?…` (no `AgentIdentity` param) |
| Sidecar → Entra | client_credentials with Blueprint secret + `AgentIdentity` extension | client_credentials with Blueprint secret only |
| Entra → Sidecar | JWT with `appid=Agent`, `xms_par_app_azp=Blueprint`, `roles=[User.Read.All]` | JWT with `appid=Blueprint`, **no** `xms_par_app_azp`, `roles=[]` |

**The proof:** The audience side-by-side compares the two `appid` values, and now they understand: **passing `AgentIdentity` is the *only* difference between minting a token *for* the parent vs. *for* the child.**

> Note: Calling Graph `/users?$top=3` with the *Blueprint* token will *also* return 200 only if the Blueprint independently has `User.Read.All`. In this demo the Blueprint deliberately does **not** have any Graph permissions, so a Blueprint-token Graph call will return `403`. That's the deeper proof that the Agent has its own permission surface, distinct from its parent.

---

## Part 3 — Cross-references

| Concept | Where to look |
|---|---|
| Agent Identity app registration | Entra admin center → Identity → Applications → Enterprise applications → search for `Demo Agent 202604271608` |
| Federated identity credential (the parent ↔ child link) | App registrations → Blueprint app → **Certificates & secrets → Federated credentials** tab. There will be a row whose subject ends with the Agent's App ID. |
| Audit row for each token issuance | Entra admin center → Identity → Monitoring & health → **Sign-in logs → Service principal sign-ins**. Filter by Application = "Demo Agent 202604271608". Open a row to see "Is Agent: Yes / Agent type: Agent Identity / Agent parent ID: Blueprint App ID". |
| The agent code | `app.py` in this repo |
| The sidecar API surface | `http://localhost:5000/openapi/v1.json` (only when `ASPNETCORE_ENVIRONMENT=Development`) |

---

## Part 4 — Mental model summary (one paragraph)

A Blueprint is a credential, but not an identity that does work. An Agent is an identity that does work, but holds no credential. The two are linked in Entra by a federated identity credential, server-side. At runtime, an auth-sidecar is the only piece holding the Blueprint's secret; it sits next to the agent code and exposes a tiny localhost HTTP API. When the agent says "give me a token for `graph` as `AgentIdentity=<Agent App ID>`", the sidecar authenticates to Entra as the Blueprint and asks Entra to mint a token *for* the named Agent. Entra checks the parentage server-side and mints a JWT whose `appid` is the Agent and whose `xms_par_app_azp` is the Blueprint. Microsoft Graph then validates that JWT independently and treats the call as the Agent. The full audit trail — including "Is Agent: Yes" and "Agent parent ID" — appears in Entra Sign-in logs against the Agent's row.
