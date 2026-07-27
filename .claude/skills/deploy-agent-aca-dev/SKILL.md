---
name: deploy-agent-aca-dev
description: 'AI-led deployment of the Entra Agent Identity demo to Azure Container Apps with the Microsoft Entra SDK auth-sidecar. Use when an engineering team wants to run this repo on Azure Container Apps as a two-container app: FastAPI agent on port 8000 plus the Entra auth-sidecar on localhost:5000. Builds the agent image in Azure Container Registry, creates the Container Apps environment, stores the Blueprint client secret as an ACA secret, adds the sidecar container, and verifies Agent Identity token + Graph calls. NOT for AKS (use deploy-agent-aks-dev), App Service (use deploy-agent-appservice-dev), or production hardening without secret rotation and ingress review. Pairs with teardown-agent-aca-dev for cleanup.'
---

# Deploy an Entra Agent ID Agent to Azure Container Apps

This skill deploys the repo's FastAPI demo as an Azure Container Apps app with two containers in one ACA revision:

- `agent`: this repo's `app.py`, listening on port `8000`.
- `sidecar`: `mcr.microsoft.com/entra-sdk/auth-sidecar:1.0.0-azurelinux3.0-distroless`, listening only on `localhost:5000`.

The agent never receives a client secret. It passes `AgentIdentity=<AGENT_APP_ID>` to the sidecar, and the sidecar authenticates to Entra using the Blueprint client secret stored as an ACA secret.

## When to Use

- The user wants to deploy this repo to **Azure Container Apps**.
- The user has a Blueprint app, Agent Identity app, and Blueprint client secret from the Entra Agent ID setup flow.
- The goal is a simple end-to-end demo: get an Agent token, decode JWT claims, call Microsoft Graph `/users`, and test the foreign-Agent rejection path.

## Do NOT Use When

- The target is AKS or another Kubernetes platform.
- The user needs Azure Workload Identity / secretless federation. This Container Apps sample intentionally uses a Blueprint client secret because the repo README documents that cross-tenant MI federation to a Blueprint is blocked.
- The user wants a runtime "agent skills" framework. This is a **deployment skill**, not a runtime tool/skill definition for the app.

## Prerequisites

1. Azure CLI logged in to the target subscription.
2. Azure RBAC: Contributor on the subscription or target resource group; Owner/User Access Administrator if role assignment to ACR requires elevation.
3. Entra objects already exist:
   - Blueprint app ID
   - Blueprint client secret
   - Agent Identity app ID parented by that Blueprint
   - Optional foreign Agent app ID for the negative test
4. The Agent Identity has Graph app permission needed by the demo, usually `User.Read.All`, admin-consented.

## Files

- [`scripts/deploy-vars.ps1.template`](./scripts/deploy-vars.ps1.template) - copy to `/tmp/deploy-aca-vars.ps1` or another local path and fill in values.
- [`scripts/deploy-aca-dev.ps1`](./scripts/deploy-aca-dev.ps1) - build, deploy, wire sidecar, and print the app URL.
- [`../teardown-agent-aca-dev/SKILL.md`](../teardown-agent-aca-dev/SKILL.md) - paired cleanup skill.

## Procedure

### Step 0 - Copy and fill deployment variables

```powershell
Copy-Item .claude/skills/deploy-agent-aca-dev/scripts/deploy-vars.ps1.template /tmp/deploy-aca-vars.ps1
notepad /tmp/deploy-aca-vars.ps1
```

Fill in at minimum:

- `$SubscriptionId`
- `$TenantId`
- `$ResourceGroup`
- `$Location`
- `$AcrName`
- `$BlueprintAppId`
- `$BlueprintClientSecret`
- `$AgentAppId`

### Step 1 - Confirm Azure context

Before mutating Azure resources, show the current account and confirm the target subscription with the user:

```powershell
az account show --query "{name:name,id:id,tenantId:tenantId}" -o table
```

If needed:

```powershell
az login --tenant <tenant-id>
az account set --subscription <subscription-id>
```

### Step 2 - Deploy

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass `
  -File .claude/skills/deploy-agent-aca-dev/scripts/deploy-aca-dev.ps1 `
  -VarsPath /tmp/deploy-aca-vars.ps1
```

The script:

1. Creates the resource group.
2. Creates ACR with admin disabled.
3. Builds this repo image with `az acr build`.
4. Creates the Container Apps environment.
5. Creates a temporary placeholder Container App with system-assigned identity.
6. Grants the Container App identity `AcrPull` on ACR.
7. Updates the Container App to run both the `agent` and `sidecar` containers.
8. Stores the Blueprint secret as an ACA secret named `blueprint-secret`.

### Step 3 - Verify

The script prints the FQDN. Open:

```text
https://<container-app-fqdn>/
```

Click:

1. **Get Agent Token** - confirm `appid` is the Agent app ID.
2. **Decode JWT** - confirm `xms_par_app_azp` points to the Blueprint and `roles` contains the Agent permission.
3. **Call Graph /users (as Agent)** - confirm Graph returns users.
4. **Try foreign Agent** - confirm Entra rejects an Agent not parented by this Blueprint.

Also verify Entra sign-in logs:

```text
Entra admin center -> Identity -> Monitoring & health -> Sign-in logs -> Service principal sign-ins
```

Filter by the Agent app. A successful row should show `Is Agent = Yes`, the Agent type, and the Blueprint parent.

## Adapt for Your Own Agent

The deploy pattern is:

```text
your agent container -> localhost:5000 -> Entra auth-sidecar -> Entra / downstream APIs
```

To adapt:

1. Replace the image built by this repo with your own agent image.
2. Keep `SIDECAR_URL=http://localhost:5000`.
3. Keep the sidecar container and `DownstreamApis__graph__*` env vars, or add your own `DownstreamApis__<api-name>__*` settings.
4. Store any credential used by the sidecar as ACA secrets, never in the agent container.

## Paired Skill

- [`teardown-agent-aca-dev`](../teardown-agent-aca-dev/SKILL.md) removes the Container App resources and optionally keeps or deletes the resource group.
