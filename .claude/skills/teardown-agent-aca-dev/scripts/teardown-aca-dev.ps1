[CmdletBinding()]
param(
    [string]$VarsPath = "/tmp/deploy-aca-vars.ps1",
    [bool]$DryRun = $true,
    [switch]$DeleteResourceGroup
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $VarsPath)) {
    throw "Vars file not found: $VarsPath"
}

. $VarsPath

function Require-Value {
    param([string]$Name, [string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -like "<*>") {
        throw "Required variable $Name is not set in $VarsPath"
    }
}

function Invoke-Step {
    param([string]$Command, [scriptblock]$Action)
    if ($DryRun) {
        Write-Host "[DRY RUN] $Command"
    }
    else {
        Write-Host $Command
        & $Action
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed: $Command"
        }
    }
}

Require-Value "SubscriptionId" $SubscriptionId
Require-Value "ResourceGroup" $ResourceGroup

Write-Host "Teardown plan (Azure Container Apps / Entra Agent ID):"
Write-Host "  Subscription:        $SubscriptionId"
Write-Host "  Resource group:      $ResourceGroup"
Write-Host "  Container App:       $ContainerAppName"
Write-Host "  Container App Env:   $ContainerAppEnvironment"
Write-Host "  ACR:                 $AcrName"
Write-Host "  Delete RG:           $($DeleteResourceGroup.IsPresent)"
Write-Host "  Dry run:             $DryRun"
Write-Host ""

& az account set --subscription $SubscriptionId
if ($LASTEXITCODE -ne 0) {
    throw "Failed to set Azure subscription $SubscriptionId"
}

if ($DeleteResourceGroup) {
    Invoke-Step "az group delete --name $ResourceGroup --yes --no-wait" {
        az group delete --name $ResourceGroup --yes --no-wait
    }
    return
}

if (-not [string]::IsNullOrWhiteSpace($ContainerAppName)) {
    Invoke-Step "az containerapp delete --name $ContainerAppName --resource-group $ResourceGroup --yes" {
        az containerapp delete --name $ContainerAppName --resource-group $ResourceGroup --yes
    }
}

if (-not [string]::IsNullOrWhiteSpace($ContainerAppEnvironment)) {
    Invoke-Step "az containerapp env delete --name $ContainerAppEnvironment --resource-group $ResourceGroup --yes" {
        az containerapp env delete --name $ContainerAppEnvironment --resource-group $ResourceGroup --yes
    }
}

if (-not [string]::IsNullOrWhiteSpace($AcrName)) {
    Invoke-Step "az acr delete --name $AcrName --resource-group $ResourceGroup --yes" {
        az acr delete --name $AcrName --resource-group $ResourceGroup --yes
    }
}

Write-Host ""
if ($DryRun) {
    Write-Host "Dry run complete. Re-run with -DryRun:`$false to delete resources."
}
else {
    Write-Host "Teardown complete."
}
