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
         `esl-admin secrets set` (AD-017), including api.token.<account> keys.

    The service binds only to ESL_INTERNAL_HOST:ESL_INTERNAL_PORT.

.PARAMETER PythonExe
    Interpreter of the deployed virtual environment.

.PARAMETER ServiceAccount
    Account the service runs as, e.g. ".\svc_esl" or "DOMAIN\svc_esl".

.EXAMPLE
    .\scripts\install-service.ps1 -PythonExe C:\SOLUM\ESL\.venv\Scripts\python.exe -ServiceAccount .\svc_esl
#>
param(
    [Parameter(Mandatory = $true)] [string] $PythonExe,
    [Parameter(Mandatory = $true)] [string] $ServiceAccount,
    [string] $ServiceName = $(if ($env:ESL_WINDOWS_SERVICE_NAME) { $env:ESL_WINDOWS_SERVICE_NAME } else { "SOLUM_ESL_PIPELINE" })
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
Write-Host "Pause/resume quiesce scheduling: sc.exe pause $ServiceName / sc.exe continue $ServiceName"
Write-Host "Verify with esl-admin status, then GET /health/ready on the internal listener."
