<#
.SYNOPSIS
    Register the SOLUM ESL pipeline as a Windows Service under the service account.

.DESCRIPTION
    Wraps pywin32's service installer (#28, FR-029). Run as an administrator on
    the host, after:
      1. the service account exists and has "Log on as a service";
      2. the environment variables from .env.production.example are set for
         that account (System > Environment, or the account's user variables);
      3. the DPAPI bundle was written *as the service account* with
         `esl-admin secrets set` (AD-017).

    API tokens do not have to be provisioned by hand: -IssueTokensFor generates
    one per account, stores it in the same bundle, and writes each value to its
    own file under -TokenDirectory with the bundle's ACL (#98). Running it again
    rotates the tokens, and the service must be restarted afterwards so it
    reloads the bundle.

    The service binds only to ESL_INTERNAL_HOST:ESL_INTERNAL_PORT.

.PARAMETER PythonExe
    Interpreter of the deployed virtual environment.

.PARAMETER ServiceAccount
    Account the service runs as, e.g. ".\svc_esl" or "DOMAIN\svc_esl".

.PARAMETER IssueTokensFor
    Optional API accounts to issue a bearer token for, e.g. "ops.alice","ops.bob".
    Each token is revealed exactly once, as a file under -TokenDirectory. Hand
    each file to its account holder and delete it; the value is not recoverable
    afterwards, only replaceable by issuing again.

.PARAMETER TokenDirectory
    Where -IssueTokensFor writes those files. Required when it is used, and it
    must already exist with an ACL you control.

.EXAMPLE
    .\scripts\install-service.ps1 -PythonExe C:\SOLUM\ESL\.venv\Scripts\python.exe -ServiceAccount .\svc_esl
#>
param(
    [Parameter(Mandatory = $true)] [string] $PythonExe,
    [Parameter(Mandatory = $true)] [string] $ServiceAccount,
    [string] $ServiceName = $(if ($env:ESL_WINDOWS_SERVICE_NAME) { $env:ESL_WINDOWS_SERVICE_NAME } else { "SOLUM_ESL_PIPELINE" }),
    [string[]] $IssueTokensFor = @(),
    [string] $TokenDirectory
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PythonExe)) { throw "Python interpreter not found: $PythonExe" }

$password = Read-Host -AsSecureString "Password for $ServiceAccount (not stored by this script)"
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($password))

$env:ESL_WINDOWS_SERVICE_NAME = $ServiceName
& $PythonExe -m esl_service.runtime.windows_service --username $ServiceAccount --password $plain --startup auto install
if ($LASTEXITCODE -ne 0) { throw "service installation failed with exit code $LASTEXITCODE" }
$plain = $null

Write-Host "Installed '$ServiceName' as $ServiceAccount. Start it with:"
Write-Host "    sc.exe start $ServiceName"

if ($IssueTokensFor.Count -gt 0) {
    if (-not $TokenDirectory) { throw "-IssueTokensFor requires -TokenDirectory" }
    if (-not (Test-Path $TokenDirectory)) {
        throw "-TokenDirectory does not exist: $TokenDirectory. Create it first, as an administrator, with an ACL you control."
    }

    # Run as the service account: the bundle is DPAPI-protected under user
    # scope (AD-017), so a token written by any other account is unreadable
    # by the service.
    $eslAdmin = Join-Path (Split-Path $PythonExe) "esl-admin.exe"
    if (-not (Test-Path $eslAdmin)) { throw "esl-admin not found next to the interpreter: $eslAdmin" }

    foreach ($account in $IssueTokensFor) {
        $out = Join-Path $TokenDirectory "$account.token"
        & $eslAdmin secrets issue-token $account --reason "environment setup for $ServiceName" --out $out
        if ($LASTEXITCODE -ne 0) { throw "issuing a token for '$account' failed with exit code $LASTEXITCODE" }
    }

    Write-Host ""
    Write-Host "Issued $($IssueTokensFor.Count) token file(s) under $TokenDirectory."
    Write-Host "Hand each file to its account holder, then delete it. Restart the service so it reloads the bundle."
}
Write-Host "Pause/resume quiesce scheduling: sc.exe pause $ServiceName / sc.exe continue $ServiceName"
Write-Host "Verify with esl-admin status, then GET /health/ready on the internal listener."
