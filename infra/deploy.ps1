<#
.SYNOPSIS
    Deploys Tender to Azure Container Apps on the free Consumption plan.

.DESCRIPTION
    Creates (or updates) a resource group, a Container Apps environment, and the
    three container apps -- mcp, backend, frontend -- in that order, because each
    needs the previous one's internal hostname. Reads its configuration from
    infra/deploy.env (copy infra/deploy.env.example). Safe to re-run: every step
    checks for an existing resource first.

    Full narrative, including the account setup this script assumes is already
    done, is in AZURE_DEPLOYMENT.md. deploy.sh is the bash equivalent.

.PARAMETER Recreate
    Deletes the three container apps before deploying. Use when changing something
    `az containerapp update` will not alter in place, such as ingress or the set of
    containers in the backend replica. Leaves the environment and resource group.

.EXAMPLE
    .\infra\deploy.ps1
.EXAMPLE
    .\infra\deploy.ps1 -Recreate
#>
[CmdletBinding()]
param(
    [switch]$Recreate
)

$ErrorActionPreference = 'Stop'

$InfraDir = $PSScriptRoot
$EnvFile = Join-Path $InfraDir 'deploy.env'
$YamlTemplate = Join-Path $InfraDir 'backend-app.yaml.template'

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

function Read-DeployEnv {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        throw "Missing $Path. Copy infra/deploy.env.example to infra/deploy.env and fill it in."
    }

    $config = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        # Split on the FIRST '=' only: an Atlas connection string contains more of
        # them in its query string, and splitting greedily truncates the password.
        $idx = $trimmed.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim()
        $config[$key] = $value
    }
    return $config
}

function Get-Required {
    param([hashtable]$Config, [string]$Key)

    $value = $Config[$Key]
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "infra/deploy.env is missing a value for $Key."
    }
    # Catch a copied-but-unedited example file before Azure does, since these
    # placeholders fail late and confusingly (a Mongo auth error at first chat,
    # not a deploy error).
    if ($value -match '^<|your-github-username|your-ollama-host|your-tailnet') {
        throw "infra/deploy.env still has the example placeholder for ${Key}: $value"
    }
    return $value
}

# How many times one az call is retried when it fails in transit rather than
# coming back with an answer. Eight rather than three because the failure this
# exists for is a per-call coin flip on a bad network path (a broken IPv6 route
# to management.azure.com resets roughly half of them), and a full deploy makes
# on the order of twenty calls -- three attempts would still lose most runs.
$script:AzRetryMax = 8

function Test-AzTransient {
    <#
        True when az never got an answer out of the network, as opposed to
        getting one it did not like.

        Matched on the message text rather than on a non-zero exit code on
        purpose: retrying every failure would retry a quota rejection, an
        "already exists", or a malformed YAML eight times with backoff and then
        report the last attempt's message instead of failing immediately with
        the first -- worse than not retrying at all.
    #>
    param([string]$Text)
    return $Text -match 'Connection aborted|ConnectionResetError|10054|RemoteDisconnected|Connection broken|Max retries exceeded|Read timed out'
}

function Wait-AzRetry {
    param([int]$Attempt)
    $delay = [int][Math]::Min(30, [Math]::Pow(2, $Attempt))
    Write-Host "    Could not reach Azure (attempt $Attempt/$($script:AzRetryMax)); retrying in ${delay}s." -ForegroundColor DarkYellow
    Start-Sleep -Seconds $delay
}

function Invoke-AzCapture {
    <#
        Runs `az` exactly once and returns @{ ExitCode; Output; Text } without
        ever throwing. Invoke-Az and Test-AzResource both build on it.

        The stderr redirect needs $ErrorActionPreference relaxed for the length
        of the call. PowerShell 5.1 wraps each stderr line of a native command
        in an ErrorRecord, and under 'Stop' the first one is a *terminating*
        error raised at the redirect itself -- so the $LASTEXITCODE check after
        it, and the readable message it throws, were unreachable, and every az
        failure surfaced as a raw NativeCommandError naming this file's line
        number instead of saying what az was doing.

        Simple function using $args, not an advanced one -- same reason as
        Invoke-Az below.
    #>
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $output = & az @args 2>&1 } finally { $ErrorActionPreference = $previous }
    return @{
        ExitCode = $LASTEXITCODE
        Output   = $output
        Text     = ($output | Out-String)
    }
}

function Invoke-Az {
    <#
        Runs `az`, retrying transport failures, and throws on anything Azure
        actually answered with. The CLI writes progress and warnings to stderr
        even on success, so this checks $LASTEXITCODE rather than treating any
        stderr output as failure -- and returns stdout *only*, so a stray
        warning line can never end up inside a `--query ... -o tsv` value the
        caller is about to .Trim() and use as a resource id.

        Deliberately a *simple* function using $args, not an advanced one with
        [Parameter(ValueFromRemainingArguments)]. That attribute makes this an
        advanced function, which gains PowerShell's common parameters -- and then
        any az short flag is parsed as one of those instead of being passed
        through. `-o tsv` fails outright with "the parameter name 'o' is
        ambiguous. Possible matches include: -OutVariable -OutBuffer", because
        PowerShell prefix-matches it against them before az is ever invoked.
        A simple function declares no parameters at all, so every token lands in
        $args verbatim and az sees exactly what was written here.
    #>
    Write-Verbose "az $($args -join ' ')"
    $attempt = 1
    while ($true) {
        $result = Invoke-AzCapture @args
        if ($result.ExitCode -eq 0) {
            return @($result.Output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })
        }
        if (-not (Test-AzTransient $result.Text) -or $attempt -ge $script:AzRetryMax) {
            throw "az $($args -join ' ') failed:`n$($result.Text.Trim())"
        }
        Wait-AzRetry -Attempt $attempt
        $attempt++
    }
}

function Test-AzResource {
    <# Existence check that distinguishes "absent" from "az could not reach
       Azure" -- which the exit-code-only version this replaces claimed to do
       but did not. A connection reset and a 404 both exit non-zero, so on a
       flaky network this script would decide an existing environment was
       missing and try to create it again, or -- under -Recreate -- silently
       skip deleting an app that was really there, producing exactly the stale
       config -Recreate exists to clear. A transport failure now retries and
       then throws; only a real answer from Azure returns $false.
       Simple function using $args for the same reason as Invoke-Az above. #>
    $attempt = 1
    while ($true) {
        $result = Invoke-AzCapture @args
        if ($result.ExitCode -eq 0) { return $true }
        if (-not (Test-AzTransient $result.Text)) { return $false }
        if ($attempt -ge $script:AzRetryMax) {
            throw "az $($args -join ' ') could not reach Azure after $($script:AzRetryMax) attempts:`n$($result.Text.Trim())"
        }
        Wait-AzRetry -Attempt $attempt
        $attempt++
    }
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

$config = Read-DeployEnv -Path $EnvFile

$ResourceGroup = Get-Required $config 'RESOURCE_GROUP'
$Location = Get-Required $config 'LOCATION'
$EnvironmentName = Get-Required $config 'ENVIRONMENT_NAME'
$GhcrOwner = (Get-Required $config 'GHCR_OWNER').ToLowerInvariant()
$ImageTag = Get-Required $config 'IMAGE_TAG'
$MongoUri = Get-Required $config 'MONGODB_URI'
$MongoDbName = Get-Required $config 'MONGODB_DB_NAME'
$OllamaBaseUrl = Get-Required $config 'OLLAMA_BASE_URL'
$OllamaModel = Get-Required $config 'OLLAMA_MODEL'
$TsClientId = Get-Required $config 'TS_CLIENT_ID'
$TsClientSecret = Get-Required $config 'TS_CLIENT_SECRET'
$TsHostname = Get-Required $config 'TS_HOSTNAME'
$MaxHistoryTurns = Get-Required $config 'MAX_HISTORY_TURNS'

$BackendImage = "ghcr.io/$GhcrOwner/tender-backend:$ImageTag"
$McpImage = "ghcr.io/$GhcrOwner/tender-mcp:$ImageTag"
$FrontendImage = "ghcr.io/$GhcrOwner/tender-frontend:$ImageTag"

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

Write-Step "Checking the Azure CLI and sign-in state"
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "The Azure CLI is not on PATH. Install it from https://aka.ms/installazurecli and re-run."
}
$account = Invoke-Az account show --query 'name' -o tsv
Write-Host "    Subscription: $account"

# The containerapp commands live in an extension. --upgrade keeps a stale local
# copy from failing on newer YAML/flag shapes this script uses.
Invoke-Az extension add --name containerapp --upgrade --only-show-errors | Out-Null

Write-Step "Registering resource providers (no-op if already registered)"
Invoke-Az provider register --namespace Microsoft.App --wait | Out-Null
Invoke-Az provider register --namespace Microsoft.OperationalInsights --wait | Out-Null

# ---------------------------------------------------------------------------
# Resource group and environment
# ---------------------------------------------------------------------------

Write-Step "Resource group: $ResourceGroup"
if (Test-AzResource group show --name $ResourceGroup) {
    Write-Host "    Already exists."
} else {
    Invoke-Az group create --name $ResourceGroup --location $Location | Out-Null
    Write-Host "    Created in $Location."
}

Write-Step "Container Apps environment: $EnvironmentName"
if (Test-AzResource containerapp env show --name $EnvironmentName --resource-group $ResourceGroup) {
    Write-Host "    Already exists."
} else {
    # --logs-destination none keeps this genuinely free: an attached Log Analytics
    # workspace bills for ingestion beyond its allowance. Live streaming via
    # `az containerapp logs show` still works without one; only historical queries
    # are lost. Switch to `--logs-destination log-analytics` if you want them and
    # accept the cost.
    Invoke-Az containerapp env create `
        --name $EnvironmentName `
        --resource-group $ResourceGroup `
        --location $Location `
        --logs-destination none | Out-Null
    Write-Host "    Created."
}

$EnvironmentId = (Invoke-Az containerapp env show `
        --name $EnvironmentName `
        --resource-group $ResourceGroup `
        --query id -o tsv).Trim()

if ($Recreate) {
    Write-Step "Deleting existing container apps (-Recreate)"
    foreach ($app in @('frontend', 'backend', 'mcp')) {
        if (Test-AzResource containerapp show --name $app --resource-group $ResourceGroup) {
            Invoke-Az containerapp delete --name $app --resource-group $ResourceGroup --yes | Out-Null
            Write-Host "    Deleted $app."
        }
    }
}

# ---------------------------------------------------------------------------
# 1/3: mcp -- innermost service, nothing depends on it being reachable to start
# ---------------------------------------------------------------------------

Write-Step "Container app 1/3: mcp"
$mcpExists = Test-AzResource containerapp show --name mcp --resource-group $ResourceGroup
if ($mcpExists) {
    Invoke-Az containerapp update `
        --name mcp `
        --resource-group $ResourceGroup `
        --image $McpImage `
        --set-env-vars "MONGODB_URI=secretref:mongodb-uri" "MONGODB_DB_NAME=$MongoDbName" "MCP_HOST=0.0.0.0" "MCP_PORT=9000" | Out-Null
    # Ingress is converged on every run, not just at create: `containerapp
    # update` does not touch it, so an mcp app created before --allow-insecure
    # was passed keeps 301-ing the backend's plain-HTTP MCP calls to HTTPS and
    # never repairs itself. The symptom is silent -- chat answers with no tools
    # -- so a deploy that cannot self-heal this would report success forever.
    Invoke-Az containerapp ingress update `
        --name mcp `
        --resource-group $ResourceGroup `
        --allow-insecure | Out-Null
    Write-Host "    Updated to $ImageTag."
} else {
    # MCP_HOST=0.0.0.0, not 127.0.0.1: the backend app connects from outside this
    # container, so a loopback bind would refuse every call. Ingress is internal,
    # so 0.0.0.0 is still only reachable from inside the environment.
    #
    # --allow-insecure matches allowInsecure: true in backend-app.yaml.template.
    # Without it the ingress answers plain HTTP with a 301 to HTTPS, and
    # MCP_SERVER_URL is http:// -- the streamable-HTTP client does not follow that
    # redirect, so discovery times out. That failure is silent by design (chat
    # answers, minus tools), which is why it is pinned rather than left to the
    # ingress default.
    Invoke-Az containerapp create `
        --name mcp `
        --resource-group $ResourceGroup `
        --environment $EnvironmentName `
        --image $McpImage `
        --ingress internal `
        --target-port 9000 `
        --allow-insecure `
        --transport auto `
        --cpu 0.25 --memory 0.5Gi `
        --min-replicas 0 --max-replicas 1 `
        --secrets "mongodb-uri=$MongoUri" `
        --env-vars "MONGODB_URI=secretref:mongodb-uri" "MONGODB_DB_NAME=$MongoDbName" "MCP_HOST=0.0.0.0" "MCP_PORT=9000" | Out-Null
    Write-Host "    Created."
}

$McpFqdn = (Invoke-Az containerapp show `
        --name mcp `
        --resource-group $ResourceGroup `
        --query 'properties.configuration.ingress.fqdn' -o tsv).Trim()
# Port 80, not 9000: internal ingress listens on 80/443 and forwards to the
# target port. Addressing 9000 here would connect to nothing.
$McpServerUrl = "http://$McpFqdn/mcp"
Write-Host "    MCP_SERVER_URL = $McpServerUrl"

# ---------------------------------------------------------------------------
# 2/3: backend -- two containers, so YAML rather than flags
# ---------------------------------------------------------------------------

Write-Step "Container app 2/3: backend (with Tailscale sidecar)"

# Rendered outside the repo and deleted in `finally`: it inlines the Mongo
# connection string and the Tailscale OAuth secret.
$RenderedYaml = Join-Path ([System.IO.Path]::GetTempPath()) "tender-backend-$([guid]::NewGuid().ToString('N')).yaml"
try {
    $yaml = Get-Content -LiteralPath $YamlTemplate -Raw
    $replacements = @{
        '__LOCATION__'          = $Location
        '__ENVIRONMENT_ID__'    = $EnvironmentId
        '__MONGODB_URI__'       = $MongoUri
        '__MONGODB_DB_NAME__'   = $MongoDbName
        '__TS_CLIENT_ID__'      = $TsClientId
        '__TS_CLIENT_SECRET__'  = $TsClientSecret
        '__TS_HOSTNAME__'       = $TsHostname
        '__BACKEND_IMAGE__'     = $BackendImage
        '__OLLAMA_BASE_URL__'   = $OllamaBaseUrl
        '__OLLAMA_MODEL__'      = $OllamaModel
        '__MAX_HISTORY_TURNS__' = $MaxHistoryTurns
        '__MCP_SERVER_URL__'    = $McpServerUrl
        # Wide open for this pass only. The frontend hostname does not exist yet,
        # and CORS cannot name a host that has not been created; step 3 narrows it.
        '__ALLOWED_ORIGINS__'   = '*'
    }
    foreach ($token in $replacements.Keys) {
        # Literal replacement, not -replace: an Atlas password or secret can
        # contain regex metacharacters, and a URI contains '$'-free but
        # '?'/'&'-heavy text that a regex would mangle.
        $yaml = $yaml.Replace($token, $replacements[$token])
    }
    if ($yaml -match '__[A-Z_]+__') {
        throw "backend-app.yaml.template still has unsubstituted placeholders: $($Matches[0])"
    }
    # WriteAllText with an explicit BOM-less encoder, not `Set-Content -Encoding
    # utf8`: on PowerShell 5.1 that switch always writes a byte-order mark, and
    # the PyYAML the Azure CLI bundles (6.0.3) rejects a BOM when it parses a
    # *stream*. The failure reads as `expected '<document start>', but found
    # '<block mapping start>'` against the first real line of the document, which
    # looks like malformed YAML and is not -- the same bytes parse clean once the
    # mark is gone.
    [System.IO.File]::WriteAllText($RenderedYaml, $yaml, (New-Object System.Text.UTF8Encoding($false)))

    if (Test-AzResource containerapp show --name backend --resource-group $ResourceGroup) {
        Invoke-Az containerapp update --name backend --resource-group $ResourceGroup --yaml $RenderedYaml | Out-Null
        Write-Host "    Updated."
    } else {
        Invoke-Az containerapp create --name backend --resource-group $ResourceGroup --yaml $RenderedYaml | Out-Null
        Write-Host "    Created."
    }
} finally {
    if (Test-Path -LiteralPath $RenderedYaml) {
        Remove-Item -LiteralPath $RenderedYaml -Force
    }
}

$BackendFqdn = (Invoke-Az containerapp show `
        --name backend `
        --resource-group $ResourceGroup `
        --query 'properties.configuration.ingress.fqdn' -o tsv).Trim()
$BackendOrigin = "http://$BackendFqdn"
Write-Host "    BACKEND_ORIGIN = $BackendOrigin"

# ---------------------------------------------------------------------------
# 3/3: frontend -- the only publicly reachable app
# ---------------------------------------------------------------------------

Write-Step "Container app 3/3: frontend"
if (Test-AzResource containerapp show --name frontend --resource-group $ResourceGroup) {
    Invoke-Az containerapp update `
        --name frontend `
        --resource-group $ResourceGroup `
        --image $FrontendImage `
        --set-env-vars "BACKEND_ORIGIN=$BackendOrigin" | Out-Null
    Write-Host "    Updated to $ImageTag."
} else {
    # External ingress on 80. Container Apps fronts this with a managed
    # certificate on its own *.azurecontainerapps.io hostname, which is where the
    # free HTTPS comes from -- no domain purchase, no cert-manager.
    Invoke-Az containerapp create `
        --name frontend `
        --resource-group $ResourceGroup `
        --environment $EnvironmentName `
        --image $FrontendImage `
        --ingress external `
        --target-port 80 `
        --cpu 0.25 --memory 0.5Gi `
        --min-replicas 0 --max-replicas 1 `
        --env-vars "BACKEND_ORIGIN=$BackendOrigin" | Out-Null
    Write-Host "    Created."
}

$FrontendFqdn = (Invoke-Az containerapp show `
        --name frontend `
        --resource-group $ResourceGroup `
        --query 'properties.configuration.ingress.fqdn' -o tsv).Trim()
$PublicUrl = "https://$FrontendFqdn"

# ---------------------------------------------------------------------------
# Close the loop: CORS can only name the frontend once it exists
# ---------------------------------------------------------------------------

Write-Step "Narrowing the backend's CORS origins to $PublicUrl"
# --container-name is mandatory here and nowhere else in this script: 'backend'
# is the only app with two containers, and without it the CLI refuses the update
# rather than guess which of them the variable belongs to. The value is the
# *container* name from backend-app.yaml.template, which happens to match the app
# name -- changing one without the other breaks this step.
Invoke-Az containerapp update `
    --name backend `
    --resource-group $ResourceGroup `
    --container-name backend `
    --set-env-vars "ALLOWED_ORIGINS=$PublicUrl" | Out-Null
Write-Host "    Done. (Browser traffic is same-origin through nginx anyway; this"
Write-Host "     closes the wildcard left open while the hostname was unknown.)"

Write-Host ""
Write-Host "Deployed." -ForegroundColor Green
Write-Host "  Public URL : $PublicUrl"
Write-Host "  Backend    : $BackendFqdn (internal)"
Write-Host "  MCP server : $McpFqdn (internal)"
Write-Host ""
Write-Host "Ollama must be running on the tailnet host for chat to answer:"
Write-Host "  $OllamaBaseUrl"
Write-Host "Live logs: az containerapp logs show -n backend -g $ResourceGroup --follow"
