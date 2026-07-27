[CmdletBinding()]
param(
    [string]$VarsPath = "/tmp/deploy-aca-vars.ps1"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $VarsPath)) {
    throw "Vars file not found: $VarsPath. Copy deploy-vars.ps1.template and fill it in first."
}

. $VarsPath

function Require-Value {
    param([string]$Name, [string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -like "<*>") {
        throw "Required variable $Name is not set in $VarsPath"
    }
}

function Az {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & az @Args
    if ($LASTEXITCODE -ne 0) {
        throw "az $($Args -join ' ') failed with exit code $LASTEXITCODE"
    }
}

Require-Value "SubscriptionId" $SubscriptionId
Require-Value "TenantId" $TenantId
Require-Value "ResourceGroup" $ResourceGroup
Require-Value "Location" $Location
Require-Value "AcrName" $AcrName
Require-Value "ContainerAppEnvironment" $ContainerAppEnvironment
Require-Value "ContainerAppName" $ContainerAppName
Require-Value "BlueprintAppId" $BlueprintAppId
Require-Value "BlueprintClientSecret" $BlueprintClientSecret
Require-Value "AgentAppId" $AgentAppId

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")
$imageRef = "$AcrName.azurecr.io/$ImageName`:$ImageTag"

Write-Host "Using repo root: $repoRoot"
Write-Host "Target subscription: $SubscriptionId"
Write-Host "Target resource group: $ResourceGroup"

Az account set --subscription $SubscriptionId

foreach ($provider in @("Microsoft.App", "Microsoft.ContainerRegistry", "Microsoft.OperationalInsights")) {
    Write-Host "Registering provider $provider if needed..."
    Az provider register --namespace $provider --wait
}

Az group create --name $ResourceGroup --location $Location --output none

$acrExists = (& az acr show --name $AcrName --resource-group $ResourceGroup --query name -o tsv 2>$null)
if (-not $acrExists) {
    Az acr create --name $AcrName --resource-group $ResourceGroup --sku $AcrSku --admin-enabled false --output none
}

Write-Host "Building image $imageRef in ACR..."
Az acr build --registry $AcrName --image "$ImageName`:$ImageTag" $repoRoot --output none

$envExists = (& az containerapp env show --name $ContainerAppEnvironment --resource-group $ResourceGroup --query name -o tsv 2>$null)
if (-not $envExists) {
    Az containerapp env create --name $ContainerAppEnvironment --resource-group $ResourceGroup --location $Location --output none
}

$appExists = (& az containerapp show --name $ContainerAppName --resource-group $ResourceGroup --query name -o tsv 2>$null)
if (-not $appExists) {
    Write-Host "Creating placeholder Container App to allocate managed identity..."
    Az containerapp create `
        --name $ContainerAppName `
        --resource-group $ResourceGroup `
        --environment $ContainerAppEnvironment `
        --image "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest" `
        --target-port 80 `
        --ingress external `
        --system-assigned `
        --output none
}

$principalId = & az containerapp identity show --name $ContainerAppName --resource-group $ResourceGroup --query principalId -o tsv
$acrId = & az acr show --name $AcrName --resource-group $ResourceGroup --query id -o tsv

if ([string]::IsNullOrWhiteSpace($principalId)) {
    Az containerapp identity assign --name $ContainerAppName --resource-group $ResourceGroup --system-assigned --output none
    $principalId = & az containerapp identity show --name $ContainerAppName --resource-group $ResourceGroup --query principalId -o tsv
}

Write-Host "Granting Container App identity AcrPull on $AcrName..."
& az role assignment create --assignee $principalId --role AcrPull --scope $acrId --output none 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "AcrPull role assignment may already exist; continuing."
}

$managedEnvId = & az containerapp env show --name $ContainerAppEnvironment --resource-group $ResourceGroup --query id -o tsv
$foreignEnv = @()
if (-not [string]::IsNullOrWhiteSpace($ForeignAgentAppId)) {
    $foreignEnv = @"
            - name: FOREIGN_AGENT_APP_ID
              value: "$ForeignAgentAppId"
"@
}

$yaml = @"
name: $ContainerAppName
type: Microsoft.App/containerApps
location: $Location
identity:
  type: SystemAssigned
properties:
  managedEnvironmentId: $managedEnvId
  configuration:
    activeRevisionsMode: Single
    ingress:
      external: true
      targetPort: 8000
      transport: Auto
      allowInsecure: false
    secrets:
      - name: blueprint-secret
        value: "$BlueprintClientSecret"
    registries:
      - server: $AcrName.azurecr.io
        identity: system
  template:
    containers:
      - name: agent
        image: $imageRef
        env:
          - name: TENANT_ID
            value: "$TenantId"
          - name: BLUEPRINT_APP_ID
            value: "$BlueprintAppId"
          - name: AGENT_APP_ID
            value: "$AgentAppId"
          - name: SIDECAR_URL
            value: "http://localhost:5000"
$foreignEnv
      - name: sidecar
        image: $SidecarImage
        env:
          - name: ASPNETCORE_ENVIRONMENT
            value: "Development"
          - name: ASPNETCORE_URLS
            value: "http://+:5000"
          - name: AzureAd__TenantId
            value: "$TenantId"
          - name: AzureAd__ClientId
            value: "$BlueprintAppId"
          - name: AzureAd__ClientCredentials__0__SourceType
            value: "ClientSecret"
          - name: AzureAd__ClientCredentials__0__ClientSecret
            secretRef: blueprint-secret
          - name: AzureAd__Instance
            value: "https://login.microsoftonline.com/"
          - name: DownstreamApis__graph__BaseUrl
            value: "https://graph.microsoft.com/v1.0"
          - name: DownstreamApis__graph__Scopes__0
            value: "https://graph.microsoft.com/.default"
          - name: DownstreamApis__graph__RequestAppToken
            value: "true"
"@

$tmpYaml = Join-Path ([System.IO.Path]::GetTempPath()) "agent-id-aca-$([guid]::NewGuid()).yaml"
try {
    Set-Content -Path $tmpYaml -Value $yaml -Encoding utf8
    Write-Host "Updating Container App with agent + sidecar containers..."
    Az containerapp update --name $ContainerAppName --resource-group $ResourceGroup --yaml $tmpYaml --output none
}
finally {
    Remove-Item -Path $tmpYaml -Force -ErrorAction SilentlyContinue
}

$fqdn = & az containerapp show --name $ContainerAppName --resource-group $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
Write-Host ""
Write-Host "Deployment complete."
Write-Host "Open: https://$fqdn/"
Write-Host "Resource group: $ResourceGroup"
Write-Host "Container App: $ContainerAppName"
