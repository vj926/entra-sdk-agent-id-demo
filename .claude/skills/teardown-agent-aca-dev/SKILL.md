---
name: teardown-agent-aca-dev
description: 'AI-led teardown for the Entra Agent Identity Azure Container Apps deployment created by deploy-agent-aca-dev. Use when the user wants to delete the Container App, Container Apps environment, ACR, and optionally the whole resource group. Dry-run by default. Does not delete Entra Blueprint or Agent Identity objects unless the operator handles that separately, because those objects may be shared across demos.'
---

# Teardown - Entra Agent ID Agent on Azure Container Apps

This skill reverses [`deploy-agent-aca-dev`](../deploy-agent-aca-dev/SKILL.md).

It removes Azure runtime resources for the Container Apps deployment:

- Container App
- Container Apps environment
- ACR
- Optional full resource group deletion

It does **not** delete the Entra Blueprint, Agent Identity, or Blueprint client secret by default. Those may be shared with other agents and should be deleted only after explicit user confirmation in the Entra admin center or Graph.

## Safety posture

1. Dry-run by default.
2. Full resource group deletion is opt-in.
3. Entra app deletion is intentionally out of scope.
4. Always confirm subscription and resource group before destructive operations.

## Prerequisites

- The same vars file used for deployment, usually `/tmp/deploy-aca-vars.ps1`.
- Azure CLI logged in to the subscription that owns the resource group.

## Procedure

### Dry run

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass `
  -File .claude/skills/teardown-agent-aca-dev/scripts/teardown-aca-dev.ps1 `
  -VarsPath /tmp/deploy-aca-vars.ps1
```

### Delete only Container Apps resources and ACR

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass `
  -File .claude/skills/teardown-agent-aca-dev/scripts/teardown-aca-dev.ps1 `
  -VarsPath /tmp/deploy-aca-vars.ps1 `
  -DryRun:$false
```

### Delete the entire resource group

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass `
  -File .claude/skills/teardown-agent-aca-dev/scripts/teardown-aca-dev.ps1 `
  -VarsPath /tmp/deploy-aca-vars.ps1 `
  -DryRun:$false `
  -DeleteResourceGroup
```

## Verification

```powershell
az containerapp show -n $ContainerAppName -g $ResourceGroup
az containerapp env show -n $ContainerAppEnvironment -g $ResourceGroup
az acr show -n $AcrName -g $ResourceGroup
```

All should return not found after a real teardown.
