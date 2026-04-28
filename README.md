# How to Publish Agents using Entra SDK

A working end-to-end demo of **Microsoft Entra Agent Identity**: a credential-less Agent app that calls Microsoft Graph through an auth-sidecar, using a parent **Blueprint** app's credential. The Agent's identity (`appid`) appears on the issued token and in Entra Sign-in logs, while the Blueprint's secret never leaves the sidecar.

> The agent code never touches a client secret. The Blueprint's secret lives only inside the auth-sidecar, and the `AgentIdentity` query parameter is the lever that makes Entra issue a token *for* the Agent.

---

## What's in this repo

| File | Purpose |
|---|---|
| `app.py` | FastAPI demo UI with 4 stepped buttons (Get Agent Token / Decode JWT / Call Graph / Negative comparison). |
| `Dockerfile` | python:3.12-slim image, exposes port 8000. |
| `.env.example` | Required environment variables for `app.py`. |

---

## How the flow works (Step 1 example)
```
<img width="1716" height="1310" alt="image" src="https://github.com/user-attachments/assets/dbe7479a-34ab-445a-aa55-ee465f6d8d6d" />

```

1. **Browser → Agent**: `GET /api/token?asAgent=true`. The browser only ever talks to `:8000`; the sidecar is private to the pod.
2. **Agent → Sidecar**: in-pod HTTP call to the sidecar's `AuthorizationHeaderUnauthenticated/graph` route. The agent passes `AgentIdentity={AGENT_APP_ID}` — this is the lever.
3. **Sidecar → Entra**: standard OAuth 2.0 client_credentials, but using the **Blueprint's** secret. Because `AgentIdentity` is set, Entra knows to mint the token for the Agent, not the Blueprint.
4. **Entra → Sidecar**: returns a JWT whose `appid` is the Agent and whose `xms_par_app_azp` is the Blueprint (the parent that authenticated). `roles` reflect the Agent's permissions.
5. **Sidecar → Agent**: returns `{ "Authorization": "Bearer <jwt>" }`.
6. **Agent → Browser**: returns the token + decoded claims so the UI can prove `appid=Agent`, `xms_par_app_azp=Blueprint`.

Step 3 (Call Graph) and Step 4 (Blueprint negative comparison) follow the same pattern; Step 4 just omits the `AgentIdentity` parameter so Entra falls back to issuing the token for the Blueprint itself — the side-by-side `appid` comparison is what makes Agent Identity tangible.

---

## Prerequisites

1. An **Entra tenant** where you have Global Admin.
2. A published **Blueprint + Agent Identity** in that tenant. The easiest path is the official sample:
   - `EntraAgentID-Functions.ps1` from the Microsoft Entra Agent ID samples.
   - `Connect-MgGraph -TenantId <tenant> -Scopes ...` (browser auth — device code may not work inside the workflow).
   - `Start-EntraAgentIDWorkflow -BlueprintName 'Demo Blueprint' -AgentName 'Demo Agent'`
   - Save the Blueprint client secret it prints — you'll mount it as a sidecar secret.
3. An Azure subscription for the runtime (Container Apps), and an Azure Container Registry for the agent image.

---

## Configure

Create `.env` from `.env.example`:

```bash
cp .env.example .env
# Fill in TENANT_ID, BLUEPRINT_APP_ID, AGENT_APP_ID
```

The Blueprint client secret is **not** an env var for the agent — it is only set on the **sidecar** as `AzureAd__ClientCredentials__0__ClientSecret` (mounted from a platform secret).

---

## Run locally (without sidecar — token call will fail, UI still renders)

```bash
pip install fastapi==0.115.0 uvicorn==0.30.6 httpx==0.27.2
uvicorn app:app --host 0.0.0.0 --port 8000
```

To exercise the end-to-end flow you need the auth-sidecar running on `localhost:5000` with the Blueprint secret. The realistic deployment is two containers in one pod (see below).

---

## Deploy on Azure Container Apps

A single ACA app with two containers: this `agent-demo` image and `mcr.microsoft.com/entra-sdk/auth-sidecar:1.0.0-azurelinux3.0-distroless`.

### 1. Build & push the image

```bash
az acr build -r <ACR_NAME> -t agent-demo:v1 .
```

### 2. Create the Container App

```bash
az containerapp create \
  -n agent-id-demo -g <RG> --environment <ACA_ENV> \
  --image <ACR_NAME>.azurecr.io/agent-demo:v1 \
  --target-port 8000 --ingress external \
  --registry-server <ACR_NAME>.azurecr.io \
  --secrets "blueprint-secret=<BLUEPRINT_CLIENT_SECRET>" \
  --env-vars TENANT_ID=<TENANT> AGENT_APP_ID=<AGENT> BLUEPRINT_APP_ID=<BLUEPRINT> SIDECAR_URL=http://localhost:5000
```

### 3. Add the auth-sidecar as a second container

Pull the YAML, append a `sidecar` container with the env below, push back with `az containerapp update --yaml`.

```yaml
- name: sidecar
  image: mcr.microsoft.com/entra-sdk/auth-sidecar:1.0.0-azurelinux3.0-distroless
  env:
    - name: ASPNETCORE_ENVIRONMENT
      value: Development
    - name: ASPNETCORE_URLS
      value: http://+:5000
    - name: AzureAd__TenantId
      value: <TENANT>
    - name: AzureAd__ClientId
      value: <BLUEPRINT_APP_ID>
    - name: AzureAd__ClientCredentials__0__SourceType
      value: ClientSecret
    - name: AzureAd__ClientCredentials__0__ClientSecret
      secretRef: blueprint-secret
    - name: AzureAd__Instance
      value: https://login.microsoftonline.com/
    - name: DownstreamApis__graph__BaseUrl
      value: https://graph.microsoft.com/v1.0
    - name: DownstreamApis__graph__Scopes__0
      value: https://graph.microsoft.com/.default
    - name: DownstreamApis__graph__RequestAppToken
      value: "true"
```

`ASPNETCORE_ENVIRONMENT=Development` exposes `/openapi/v1.json`, useful for inspecting the sidecar API. Drop it for production.

---

## Verify in Entra Sign-in logs

Each token mint shows up in **Entra admin center → Identity → Monitoring & health → Sign-in logs → Service principal sign-ins**. Open the row corresponding to the Agent app to see:

- **Is Agent**: `Yes`
- **Agent type**: `Agent Identity`
- **Agent parent ID**: the Blueprint App ID
- **Client credential type**: `Federated identity credential` or `ClientSecret`
- **Unique token identifier**: matches the `uti` claim shown in the demo UI

This is the proof artifact: authentication used the Blueprint's credential, but the resulting token (and audit row) is recorded against the Agent.

---

## Gotchas (from real deployment)

1. The sidecar's MSAL cache key is `(client_id, scope, tenant)` — `AgentIdentity` is **not** part of the key. Once a token is cached for one identity, later calls return the cached one. Workaround in this demo: snapshot both tokens at startup, before any cache pollution.
2. `az containerapp revision restart` does not actually cycle the pod. To reset the in-memory MSAL cache, deploy a new revision (e.g., bump a `CACHE_BUST` env var).
3. The sidecar accepts `optionsOverride.AcquireTokenOptions.CorrelationId` as a query parameter but does not forward it to MSAL/Entra in `1.0.0-azurelinux3.0-distroless`. Use the `uti` claim + Application + time window to match Sign-in log rows.
4. Cross-tenant federated identity from an Azure MI to an Entra Blueprint is blocked (`AADSTS700236`). Use the Blueprint's client secret stored as an ACA secret.
5. Device code auth fails inside the official `Start-EntraAgentIDWorkflow` script — use interactive browser auth for `Connect-MgGraph`.

---

## Architecture diagram

```
┌─────────────────────── ACA Pod ───────────────────────┐
│                                                       │
│  ┌──── agent (FastAPI, :8000) ────┐                   │
│  │  HTML UI + /api/* endpoints     │ ◄── public HTTPS │
│  └────────────────┬───────────────┘                   │
│                   │ localhost                         │
│  ┌────────────────▼───────────────┐                   │
│  │ auth-sidecar (:5000)            │ ── Entra/Graph   │
│  │ holds Blueprint secret only     │                  │
│  └────────────────────────────────┘                   │
└───────────────────────────────────────────────────────┘
```

---

## License

MIT. See `LICENSE` if added.
