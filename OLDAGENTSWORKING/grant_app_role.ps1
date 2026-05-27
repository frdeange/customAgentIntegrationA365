# =============================================================================
# Grant the Agent365.Observability.OtelWrite APP ROLE to our agent identity.
#
# Background:
#   `a365 setup all` only attempted a delegated permission grant
#   (oauth2PermissionGrants), but the Agent 365 ingest endpoint actually
#   requires an APP ROLE assignment because the call is app-only (no user).
#   That's why we got HTTP 403 "insufficient_scope" — token was valid,
#   but missing the role.
#
# This script:
#   1. Connects to Microsoft Graph with the scopes needed to manage app role
#      assignments.
#   2. Creates the Observability API service principal in the tenant if it
#      doesn't exist yet (`9b975845-388f-4429-889e-eab1ef63949c`).
#   3. Looks up the `Agent365.Observability.OtelWrite` app role on it.
#   4. Assigns that role to our agent identity service principal.
#
# Run from a foreground PowerShell window so WAM/interactive auth can prompt.
# =============================================================================

$ErrorActionPreference = 'Stop'

$TenantId    = '562029ef-9022-45a6-b255-40cd71ebb2ce'
$AgentAppId  = 'df0a8197-53a7-4331-b4b0-7cde09f81203'   # our agent identity
$ObsApiAppId = '9b975845-388f-4429-889e-eab1ef63949c'   # Agent 365 Observability API
$RoleName    = 'Agent365.Observability.OtelWrite'

Write-Host "Connecting to Microsoft Graph..." -ForegroundColor Cyan
Connect-MgGraph `
    -TenantId $TenantId `
    -Scopes 'AppRoleAssignment.ReadWrite.All','Application.ReadWrite.All','Directory.Read.All' `
    -NoWelcome

Write-Host "`nResolving agent identity service principal ($AgentAppId)..." -ForegroundColor Cyan
$agentSp = Get-MgServicePrincipal -Filter "appId eq '$AgentAppId'"
if (-not $agentSp) {
    throw "Agent identity SP not found in tenant. Did `a365 setup all` finish OK?"
}
Write-Host "  Agent SP object id: $($agentSp.Id)"

Write-Host "`nResolving Observability API service principal ($ObsApiAppId)..." -ForegroundColor Cyan
$obsSp = Get-MgServicePrincipal -Filter "appId eq '$ObsApiAppId'"
if (-not $obsSp) {
    Write-Host "  Observability API SP not present in tenant — creating it..." -ForegroundColor Yellow
    $obsSp = New-MgServicePrincipal -AppId $ObsApiAppId
    Start-Sleep -Seconds 5
}
Write-Host "  Observability SP object id: $($obsSp.Id)"

Write-Host "`nLooking up app role '$RoleName' on Observability API..." -ForegroundColor Cyan
$role = $obsSp.AppRoles | Where-Object { $_.Value -eq $RoleName }
if (-not $role) {
    Write-Host "  AppRoles available on this SP:" -ForegroundColor Yellow
    $obsSp.AppRoles | Format-Table Value, DisplayName
    throw "App role '$RoleName' not found on Observability API SP."
}
Write-Host "  Role id: $($role.Id)"

Write-Host "`nAssigning role to agent identity..." -ForegroundColor Cyan
$existing = Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $agentSp.Id `
    | Where-Object { $_.ResourceId -eq $obsSp.Id -and $_.AppRoleId -eq $role.Id }
if ($existing) {
    Write-Host "  Already assigned (assignment id: $($existing.Id)). Nothing to do." -ForegroundColor Green
} else {
    $assignment = New-MgServicePrincipalAppRoleAssignment `
        -ServicePrincipalId $agentSp.Id `
        -PrincipalId       $agentSp.Id `
        -ResourceId        $obsSp.Id `
        -AppRoleId         $role.Id
    Write-Host "  Assigned. Assignment id: $($assignment.Id)" -ForegroundColor Green
}

Write-Host "`nDone. Wait ~1-2 minutes for propagation, then re-run:" -ForegroundColor Cyan
Write-Host "    .\.venv\Scripts\python.exe agent_a365.py" -ForegroundColor White
