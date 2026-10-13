<#
  One-off OAuth spike for Trello (Phase 1, Task 1). The user runs it; it prints only status lines.
  The client secret and tokens stay in memory and are never printed or written to disk.
  Usage: pwsh scripts/oauth_spike.ps1 -ClientId <client id from trello.com/apps/admin>
#>
param([Parameter(Mandatory = $true)][string]$ClientId)

function ConvertTo-Base64Url([byte[]]$Bytes) {
    [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

$secure = Read-Host 'Client secret (input hidden)' -AsSecureString
$secret = [Net.NetworkCredential]::new('', $secure).Password

$bytes = [byte[]]::new(48)
[Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$verifier = ConvertTo-Base64Url $bytes
$challenge = ConvertTo-Base64Url ([Security.Cryptography.SHA256]::HashData([Text.Encoding]::ASCII.GetBytes($verifier)))
$redirect = 'https://my.home-assistant.io/redirect/oauth'
$query = [ordered]@{
    client_id             = $ClientId
    scope                 = 'read:member:trello read:board:trello write:board:trello offline_access'
    redirect_uri          = $redirect
    response_type         = 'code'
    prompt                = 'consent'
    state                 = 'spike'
    code_challenge        = $challenge
    code_challenge_method = 'S256'
}
$qs = ($query.GetEnumerator() | ForEach-Object { "$($_.Key)=$([uri]::EscapeDataString($_.Value))" }) -join '&'
Start-Process "https://auth.atlassian.com/authorize?$qs"

Write-Host 'Q1: if Atlassian shows an error about redirect_uri / callback URL, Q1 = NOT ACCEPTED (stop here).'
$pasted = (Read-Host 'After approving, paste the whole address-bar URL (or just the code) within 5 minutes').Trim()
$code = if ($pasted -match 'code=([^&\s]+)') { $Matches[1] } else { $pasted }

function Get-ErrorText($err) {
    $status = $err.Exception.Response.StatusCode.value__
    $detail = $err.ErrorDetails.Message  # Atlassian's JSON error body (error / error_description) - no secrets
    "$status $detail"
}

$tokenUrl = 'https://auth.atlassian.com/oauth/token'
$body = @{
    grant_type    = 'authorization_code'
    client_id     = $ClientId
    client_secret = $secret
    code          = $code
    redirect_uri  = $redirect
    code_verifier = $verifier
}
$token = $null
try {
    $token = Invoke-RestMethod -Method Post -Uri $tokenUrl -Body $body -ContentType 'application/x-www-form-urlencoded'
    Write-Host 'Q2: form-encoded token request -> OK'
} catch {
    Write-Host "Q2: form-encoded token request -> FAILED ($(Get-ErrorText $_))"
    try {
        $token = Invoke-RestMethod -Method Post -Uri $tokenUrl -Body ($body | ConvertTo-Json) -ContentType 'application/json'
        Write-Host 'Q2: JSON token request -> OK'
    } catch {
        Write-Host "Q2: JSON token request -> FAILED ($(Get-ErrorText $_))"
    }
}
if ($token) {
    Write-Host "    refresh token returned: $([bool]$token.refresh_token); expires_in: $($token.expires_in)"
    $headers = @{ Authorization = "Bearer $($token.access_token)" }
    foreach ($base in 'https://api.trello.com/1', 'https://trello.com/1') {
        try {
            Invoke-RestMethod -Uri "$base/members/me?fields=id" -Headers $headers | Out-Null
            Write-Host "Q3: $base -> OK"
        } catch {
            Write-Host "Q3: $base -> FAILED ($($_.Exception.Response.StatusCode.value__))"
        }
    }
    # Q4: can the token read boards in every workspace (e.g. a board in someone else's workspace)?
    try {
        $boards = Invoke-RestMethod -Uri 'https://api.trello.com/1/members/me/boards?filter=open&fields=name' -Headers $headers
        foreach ($b in $boards) {
            try {
                Invoke-RestMethod -Uri "https://api.trello.com/1/boards/$($b.id)/lists?fields=name" -Headers $headers | Out-Null
                Write-Host "Q4: board '$($b.name)' -> readable"
            } catch {
                Write-Host "Q4: board '$($b.name)' -> FAILED ($($_.Exception.Response.StatusCode.value__))"
            }
        }
    } catch {
        Write-Host "Q4: listing boards -> FAILED ($($_.Exception.Response.StatusCode.value__))"
    }
}
$secret = $null; $token = $null
