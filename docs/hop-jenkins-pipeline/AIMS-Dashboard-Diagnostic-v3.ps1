<#
.SYNOPSIS
    Read-only diagnostic for the SOLUM AIMS Dashboard Service OpenAPI endpoint.

.DESCRIPTION
    Performs network and OpenAPI inspection against the AIMS Dashboard Service,
    automatically searches read-only Article GET endpoints for one existing
    article value, and then tests GET /common/articles/content.

    Safety properties:
      - HTTP GET requests only.
      - No database commands.
      - No modifying API calls.
      - No AIMS/database credentials are read or printed.

.NOTES
    Default environment:
      AIMS host : 192.168.85.213
      Port      : 9001
      Store     : 084
#>

[CmdletBinding()]
param(
    [string]$AimsHost = '192.168.85.213',
    [int]$AimsPort = 9001,
    [string]$Store = '084',
    [string]$ReportDirectory = (Get-Location).Path,
    [int]$TimeoutSec = 30,
    [int]$MaxDiscoveryRequests = 8
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$DashboardBasePath = '/dashboardservice'
$OpenApiPath = '/dashboardservice/doc/json/common'
$TargetPath = '/common/articles/content'
$BaseAuthority = "http://${AimsHost}:$AimsPort"
$DashboardBaseUrl = "$BaseAuthority$DashboardBasePath"
$OpenApiUrl = "$BaseAuthority$OpenApiPath"
$TargetUrl = "$DashboardBaseUrl$TargetPath"
$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'

if (-not (Test-Path -LiteralPath $ReportDirectory)) {
    New-Item -ItemType Directory -Path $ReportDirectory -Force | Out-Null
}

$ReportPath = Join-Path $ReportDirectory "AIMS-Dashboard-Diagnostic-$Timestamp.txt"
New-Item -ItemType File -Path $ReportPath -Force | Out-Null

function Write-Report {
    param(
        [AllowNull()]
        [object]$Message = '',
        [ValidateSet('INFO','PASS','WARN','FAIL','DATA')]
        [string]$Level = 'INFO'
    )

    $text = if ($null -eq $Message) { '<null>' } else { [string]$Message }
    $line = '[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $text
    Write-Host $line
    Add-Content -LiteralPath $ReportPath -Value $line -Encoding UTF8
}

function Write-Section {
    param([string]$Title)
    Write-Report ''
    Write-Report ('=' * 78)
    Write-Report $Title 'DATA'
    Write-Report ('=' * 78)
}

function Convert-ToReportJson {
    param(
        [AllowNull()]
        [object]$Value,
        [int]$Depth = 20
    )

    if ($null -eq $Value) {
        return '<null>'
    }

    try {
        return ($Value | ConvertTo-Json -Depth $Depth)
    }
    catch {
        return "<unable to serialize: $($_.Exception.Message)>"
    }
}

function Get-SafePropertyValue {
    param(
        [AllowNull()][object]$Object,
        [Parameter(Mandatory=$true)][string]$Name
    )

    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Get-RedactedPreview {
    param([AllowNull()][object]$Value)

    if ($null -eq $Value) { return '<null>' }
    $s = [string]$Value
    if ([string]::IsNullOrWhiteSpace($s)) { return '<empty>' }
    if ($s.Length -le 4) { return ('*' * $s.Length) }
    if ($s.Length -le 8) { return ($s.Substring(0,2) + ('*' * ($s.Length - 2))) }
    return ($s.Substring(0,4) + ('*' * [Math]::Min(8, $s.Length - 6)) + $s.Substring($s.Length - 2,2))
}

function Convert-HeadersForReport {
    param([AllowNull()][object]$Headers)

    if ($null -eq $Headers) { return @{} }

    $safe = [ordered]@{}
    foreach ($key in $Headers.Keys) {
        $name = [string]$key
        if ($name -match '^(?i:Set-Cookie|Cookie|Authorization|Proxy-Authorization)$') {
            $safe[$name] = '<redacted>'
        }
        else {
            $safe[$name] = [string]$Headers[$key]
        }
    }
    return $safe
}

function Get-ErrorResponseContent {
    param([AllowNull()][object]$Response)

    if ($null -eq $Response) { return $null }

    try {
        if ($Response.PSObject.Methods.Name -contains 'GetResponseStream') {
            $stream = $Response.GetResponseStream()
            if ($null -ne $stream) {
                $reader = New-Object System.IO.StreamReader($stream)
                try { return $reader.ReadToEnd() }
                finally {
                    $reader.Dispose()
                    $stream.Dispose()
                }
            }
        }
    }
    catch {}

    try {
        if ($null -ne $Response.Content) {
            if ($Response.Content -is [string]) { return [string]$Response.Content }
            if ($Response.Content.PSObject.Methods.Name -contains 'ReadAsStringAsync') {
                return $Response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            }
        }
    }
    catch {}

    return $null
}

function Invoke-ReadOnlyGet {
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [int]$RequestTimeoutSec = 30
    )

    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $response = Invoke-WebRequest -Uri $Uri -Method Get -UseBasicParsing -TimeoutSec $RequestTimeoutSec -ErrorAction Stop
        $watch.Stop()
        return [pscustomobject]@{
            Uri         = $Uri
            StatusCode  = [int]$response.StatusCode
            Headers     = $response.Headers
            Content     = [string]$response.Content
            ElapsedMs   = $watch.ElapsedMilliseconds
            TransportOk = $true
            Error       = $null
        }
    }
    catch {
        $watch.Stop()
        $response = $_.Exception.Response
        $status = $null
        $headers = $null
        $content = $null

        if ($null -ne $response) {
            try {
                if ($null -ne $response.StatusCode) { $status = [int]$response.StatusCode }
            } catch {}
            try { $headers = $response.Headers } catch {}
            $content = Get-ErrorResponseContent -Response $response
        }

        return [pscustomobject]@{
            Uri         = $Uri
            StatusCode  = $status
            Headers     = $headers
            Content     = $content
            ElapsedMs   = $watch.ElapsedMilliseconds
            TransportOk = ($null -ne $status)
            Error       = $_.Exception.Message
        }
    }
}

function ConvertTo-QueryString {
    param([hashtable]$Parameters)

    $pairs = [System.Collections.Generic.List[string]]::new()
    foreach ($key in ($Parameters.Keys | Sort-Object)) {
        $value = $Parameters[$key]
        if ($null -eq $value) { continue }

        if (($value -is [System.Collections.IEnumerable]) -and -not ($value -is [string])) {
            foreach ($item in $value) {
                $pairs.Add(('{0}={1}' -f [uri]::EscapeDataString([string]$key), [uri]::EscapeDataString([string]$item)))
            }
        }
        else {
            $pairs.Add(('{0}={1}' -f [uri]::EscapeDataString([string]$key), [uri]::EscapeDataString([string]$value)))
        }
    }
    return ($pairs -join '&')
}

function Resolve-LocalSchemaRef {
    param(
        [Parameter(Mandatory=$true)][object]$Spec,
        [AllowNull()][object]$Schema
    )

    if ($null -eq $Schema) { return $null }
    $refProperty = $Schema.PSObject.Properties['$ref']
    if ($null -eq $refProperty) { return $Schema }

    $ref = [string]$refProperty.Value
    if ($ref -match '^#/components/schemas/(.+)$') {
        $schemaName = $Matches[1]
        return $Spec.components.schemas.PSObject.Properties[$schemaName].Value
    }
    return $Schema
}

function Get-PageableQuery {
    param([Parameter(Mandatory=$true)][object]$Spec)

    $result = @{}
    $pageableProperty = $Spec.components.schemas.PSObject.Properties['Pageable']
    if ($null -eq $pageableProperty) {
        $result['page'] = 0
        $result['size'] = 10
        return $result
    }

    $pageable = Resolve-LocalSchemaRef -Spec $Spec -Schema $pageableProperty.Value
    $props = $pageable.PSObject.Properties['properties']
    if ($null -eq $props -or $null -eq $props.Value) {
        $result['page'] = 0
        $result['size'] = 10
        return $result
    }

    foreach ($property in $props.Value.PSObject.Properties) {
        $name = [string]$property.Name
        $schema = Resolve-LocalSchemaRef -Spec $Spec -Schema $property.Value
        $defaultProperty = $schema.PSObject.Properties['default']
        $exampleProperty = $schema.PSObject.Properties['example']

        if ($null -ne $defaultProperty -and $null -ne $defaultProperty.Value) {
            $result[$name] = $defaultProperty.Value
            continue
        }
        if ($null -ne $exampleProperty -and $null -ne $exampleProperty.Value) {
            $result[$name] = $exampleProperty.Value
            continue
        }

        switch -Regex ($name) {
            '^(page|pageNumber)$' { $result[$name] = 0; continue }
            '^(size|pageSize)$'   { $result[$name] = 10; continue }
            '^sort$'              { continue }
        }
    }

    if ($result.Count -eq 0) {
        $result['page'] = 0
        $result['size'] = 10
    }
    return $result
}

function Get-ParameterKnownValue {
    param(
        [Parameter(Mandatory=$true)][object]$Parameter,
        [Parameter(Mandatory=$true)][object]$Spec,
        [string]$StoreCode
    )

    $name = [string]$Parameter.name
    $schema = Resolve-LocalSchemaRef -Spec $Spec -Schema $Parameter.schema

    if ($name -eq 'store') { return [pscustomobject]@{ Known=$true; Value=$StoreCode } }
    if ($name -eq 'export') { return [pscustomobject]@{ Known=$true; Value='false' } }
    if ($name -eq 'data') { return [pscustomobject]@{ Known=$true; Value='false' } }
    if ($name -match '^(page|pageNumber)$') { return [pscustomobject]@{ Known=$true; Value=0 } }
    if ($name -match '^(size|pageSize)$') { return [pscustomobject]@{ Known=$true; Value=10 } }

    foreach ($source in @($Parameter, $schema)) {
        if ($null -eq $source) { continue }
        foreach ($propName in @('example','default')) {
            $p = $source.PSObject.Properties[$propName]
            if ($null -ne $p -and $null -ne $p.Value -and -not [string]::IsNullOrWhiteSpace([string]$p.Value)) {
                return [pscustomobject]@{ Known=$true; Value=$p.Value }
            }
        }

        $enumProperty = $source.PSObject.Properties['enum']
        if ($null -ne $enumProperty -and $null -ne $enumProperty.Value -and @($enumProperty.Value).Count -gt 0) {
            return [pscustomobject]@{ Known=$true; Value=@($enumProperty.Value)[0] }
        }
    }

    return [pscustomobject]@{ Known=$false; Value=$null }
}

function New-OperationQuery {
    param(
        [Parameter(Mandatory=$true)][object]$Operation,
        [Parameter(Mandatory=$true)][object]$Spec,
        [string]$StoreCode,
        [switch]$DiscoveryMode
    )

    $query = @{}
    $reasons = [System.Collections.Generic.List[string]]::new()
    $parameters = @((Get-SafePropertyValue -Object $Operation -Name 'parameters'))

    foreach ($parameter in $parameters) {
        if ($null -eq $parameter) { continue }
        $location = [string]$parameter.in
        $name = [string]$parameter.name
        $required = [bool]$parameter.required

        if ($location -eq 'path') {
            $reasons.Add("path parameter '$name' requires an unknown path value")
            continue
        }

        if ($location -ne 'query') {
            if ($required) { $reasons.Add("required non-query parameter '$name' cannot be derived safely") }
            continue
        }

        if ($name -eq 'pageable') {
            $pageable = Get-PageableQuery -Spec $Spec
            foreach ($k in $pageable.Keys) { $query[$k] = $pageable[$k] }
            continue
        }

        $known = Get-ParameterKnownValue -Parameter $parameter -Spec $Spec -StoreCode $StoreCode
        if ($known.Known) {
            $query[$name] = $known.Value
        }
        elseif ($required) {
            $reasons.Add("required query parameter '$name' has no documented default/example/enum")
        }
    }

    return [pscustomobject]@{
        CanInvoke = ($reasons.Count -eq 0)
        Query     = $query
        Reasons   = $reasons.ToArray()
    }
}

function Get-ArticleDiscoveryOperations {
    param(
        [Parameter(Mandatory=$true)][object]$Spec,
        [string]$ExcludedPath
    )

    $items = [System.Collections.Generic.List[object]]::new()
    foreach ($pathProperty in $Spec.paths.PSObject.Properties) {
        $path = [string]$pathProperty.Name
        if ($path -eq $ExcludedPath) { continue }

        $getProperty = $pathProperty.Value.PSObject.Properties['get']
        if ($null -eq $getProperty -or $null -eq $getProperty.Value) { continue }

        $operation = $getProperty.Value
        $tags = @($operation.tags)
        if (-not ($tags -contains 'Article')) { continue }

        $score = 0
        $summary = [string]$operation.summary
        if ($summary -match '(?i)all|list|retrieve|get article|search') { $score += 20 }
        if ($path -match '(?i)/articles?$') { $score += 20 }
        if ($path -match '(?i)content') { $score += 5 }
        if (@((Get-SafePropertyValue -Object $operation -Name 'parameters') | Where-Object { $_.in -eq 'path' }).Count -eq 0) { $score += 30 }
        $requiredUnknownCount = @((Get-SafePropertyValue -Object $operation -Name 'parameters') | Where-Object {
            $_.required -eq $true -and $_.in -eq 'query' -and $_.name -notin @('store','pageable','page','pageNumber','size','pageSize','export','data')
        }).Count
        $score -= (10 * $requiredUnknownCount)

        $items.Add([pscustomobject]@{
            Path      = $path
            Operation = $operation
            Score     = $score
            Summary   = $summary
        })
    }

    return @($items | Sort-Object -Property @{Expression='Score';Descending=$true}, @{Expression='Path';Descending=$false})
}

function Test-IsScalarValue {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return $false }
    return ($Value -is [string] -or $Value -is [char] -or $Value -is [bool] -or
            $Value -is [byte] -or $Value -is [int16] -or $Value -is [int32] -or
            $Value -is [int64] -or $Value -is [uint16] -or $Value -is [uint32] -or
            $Value -is [uint64] -or $Value -is [single] -or $Value -is [double] -or
            $Value -is [decimal] -or $Value -is [datetime])
}

function Get-ObjectArticleFields {
    param([Parameter(Mandatory=$true)][object]$Object)

    $priorityNames = @('articleId','articleID','articleCode','articleCd','sku','skuId','productId','productCode','id','code')
    $found = [System.Collections.Generic.List[object]]::new()
    $properties = @($Object.PSObject.Properties)

    foreach ($preferred in $priorityNames) {
        foreach ($property in $properties) {
            if ($property.Name -ieq $preferred -and (Test-IsScalarValue $property.Value) -and -not [string]::IsNullOrWhiteSpace([string]$property.Value)) {
                $found.Add([pscustomobject]@{
                    Key   = [string]$property.Name
                    Value = $property.Value
                })
            }
        }
    }

    return $found.ToArray()
}

function Find-ArticleCandidate {
    param(
        [AllowNull()][object]$Node,
        [int]$Depth = 0
    )

    if ($null -eq $Node -or $Depth -gt 14) { return $null }

    if ($Node -is [string] -or (Test-IsScalarValue $Node)) { return $null }

    if (($Node -is [System.Collections.IEnumerable]) -and -not ($Node -is [System.Collections.IDictionary])) {
        foreach ($item in $Node) {
            $found = Find-ArticleCandidate -Node $item -Depth ($Depth + 1)
            if ($null -ne $found) { return $found }
        }
        return $null
    }

    $fields = Get-ObjectArticleFields -Object $Node
    if (@($fields).Count -gt 0) {
        $propertyNames = @($Node.PSObject.Properties.Name)
        $contextCount = @($propertyNames | Where-Object { $_ -match '(?i)article|product|sku|name|price|store|company|code|id' }).Count
        if ($contextCount -ge 2) {
            return [pscustomobject]@{
                Object         = $Node
                CandidateFields = @($fields)
                PropertyNames  = $propertyNames
            }
        }
    }

    foreach ($property in $Node.PSObject.Properties) {
        $found = Find-ArticleCandidate -Node $property.Value -Depth ($Depth + 1)
        if ($null -ne $found) { return $found }
    }
    return $null
}

function Convert-ContentToObject {
    param([AllowNull()][string]$Content)
    if ([string]::IsNullOrWhiteSpace($Content)) { return $null }
    try { return ($Content | ConvertFrom-Json -ErrorAction Stop) }
    catch { return $null }
}

function Get-ResponseClassification {
    param([Parameter(Mandatory=$true)][object]$Result)

    if (-not $Result.TransportOk) {
        return 'Transport failure: TCP/HTTP request did not receive an HTTP response.'
    }
    if ($null -eq $Result.StatusCode) {
        return 'HTTP response was detected but its status code could not be determined.'
    }

    switch ($Result.StatusCode) {
        { $_ -ge 200 -and $_ -lt 300 } { return 'PASS: remote HTTP API request completed successfully.' }
        400 { return 'REACHABLE: AIMS processed the request but rejected request parameters/content.' }
        401 { return 'REACHABLE: AIMS requires authentication for this request.' }
        403 { return 'REACHABLE: AIMS received the request but authorization/access policy denied it.' }
        404 { return 'REACHABLE: HTTP service responded, but this route was not found.' }
        405 { return 'REACHABLE: route responded but the HTTP method was not accepted.' }
        { $_ -ge 500 } { return 'REACHABLE: AIMS returned a server-side failure while processing the request.' }
        default { return "REACHABLE: AIMS returned HTTP status $($Result.StatusCode)." }
    }
}

function Get-BodyPreview {
    param([AllowNull()][string]$Content, [int]$MaxLength = 4000)
    if ([string]::IsNullOrWhiteSpace($Content)) { return '<empty>' }
    if ($Content.Length -le $MaxLength) { return $Content }
    return ($Content.Substring(0, $MaxLength) + "`n<response truncated; total characters=$($Content.Length)>")
}

Write-Report 'SOLUM AIMS Dashboard Service read-only diagnostic started.'
Write-Report "AIMS endpoint: $DashboardBaseUrl"
Write-Report "Store: $Store"
Write-Report "Report: $ReportPath"
Write-Report 'Safety mode: HTTP GET requests only; no database commands or modifying API methods.'

# STEP 1 - Network reachability
Write-Section 'STEP 1 - TCP reachability to AIMS Dashboard Service'
$tcpOk = $false
try {
    $tcp = Test-NetConnection -ComputerName $AimsHost -Port $AimsPort -WarningAction SilentlyContinue
    $tcpOk = [bool]$tcp.TcpTestSucceeded
    Write-Report "RemoteAddress: $($tcp.RemoteAddress)" 'DATA'
    Write-Report "RemotePort: $($tcp.RemotePort)" 'DATA'
    Write-Report "TcpTestSucceeded: $tcpOk" $(if ($tcpOk) {'PASS'} else {'FAIL'})
}
catch {
    Write-Report "Test-NetConnection failed: $($_.Exception.Message)" 'FAIL'
}

# STEP 2 - OpenAPI specification
Write-Section 'STEP 2 - Download and identify AIMS OpenAPI specification'
$spec = $null
try {
    $spec = Invoke-RestMethod -Uri $OpenApiUrl -Method Get -TimeoutSec $TimeoutSec -ErrorAction Stop
    Write-Report "OpenAPI URL: $OpenApiUrl" 'DATA'
    Write-Report "OpenAPI version: $($spec.openapi)" 'PASS'
    Write-Report "API title: $($spec.info.title)" 'DATA'
    Write-Report "AIMS version/build: $($spec.info.version)" 'DATA'
}
catch {
    Write-Report "Unable to load OpenAPI specification: $($_.Exception.Message)" 'FAIL'
    Write-Report 'Diagnostic cannot continue without the OpenAPI document.' 'FAIL'
    Write-Report "FINAL REPORT: $ReportPath" 'DATA'
    exit 2
}

# STEP 3 - Servers
Write-Section 'STEP 3 - OpenAPI server definitions'
foreach ($server in @($spec.servers)) {
    Write-Report ("Server URL: {0} | Description: {1}" -f $server.url, $server.description) 'DATA'
}
$relativeServer = @($spec.servers | Where-Object { [string]$_.url -like '/*' })
if ($relativeServer.Count -gt 0) {
    Write-Report 'Relative /dashboardservice server definition is present; remote clients are not inherently limited to localhost by this OpenAPI document.' 'PASS'
}
else {
    Write-Report 'No relative server definition was found.' 'WARN'
}

# STEP 4 - Target route/method
Write-Section 'STEP 4 - Inspect target Article endpoint'
$targetPathProperty = $spec.paths.PSObject.Properties[$TargetPath]
if ($null -eq $targetPathProperty) {
    Write-Report "Target route does not exist in OpenAPI: $TargetPath" 'FAIL'
    Write-Report "FINAL REPORT: $ReportPath" 'DATA'
    exit 3
}
$targetGetProperty = $targetPathProperty.Value.PSObject.Properties['get']
if ($null -eq $targetGetProperty) {
    Write-Report "Target route exists but GET is not defined: $TargetPath" 'FAIL'
    Write-Report "FINAL REPORT: $ReportPath" 'DATA'
    exit 4
}
$targetOp = $targetGetProperty.Value
Write-Report "Path: $TargetPath" 'DATA'
Write-Report 'HTTP method: GET' 'PASS'
Write-Report "Summary: $($targetOp.summary)" 'DATA'
Write-Report "operationId: $($targetOp.operationId)" 'DATA'
Write-Report "Tags: $(@($targetOp.tags) -join ', ')" 'DATA'

# STEP 5 - Parameters
Write-Section 'STEP 5 - Target query parameters'
foreach ($parameter in @($targetOp.parameters)) {
    $schema = Resolve-LocalSchemaRef -Spec $spec -Schema $parameter.schema
    $type = if ($null -ne $schema -and $null -ne $schema.PSObject.Properties['type']) { $schema.type } else { '<object/ref>' }
    Write-Report ("Name={0}; in={1}; required={2}; type={3}" -f $parameter.name, $parameter.in, $parameter.required, $type) 'DATA'
}

# STEP 6 - Pageable
Write-Section 'STEP 6 - Pageable schema and derived pagination query'
$pageableProperty = $spec.components.schemas.PSObject.Properties['Pageable']
if ($null -ne $pageableProperty) {
    Write-Report (Convert-ToReportJson -Value $pageableProperty.Value -Depth 20) 'DATA'
}
else {
    Write-Report 'Pageable schema not present; using conservative page=0 and size=10 fallback.' 'WARN'
}
$pageableQuery = Get-PageableQuery -Spec $spec
Write-Report ("Derived pagination query: " + ((($pageableQuery.Keys | Sort-Object) | ForEach-Object { "$_=$($pageableQuery[$_])" }) -join '&')) 'DATA'

# STEP 7 - Security/request-body/responses
Write-Section 'STEP 7 - Security, request body, and documented responses'
Write-Report ('Operation security: ' + (Convert-ToReportJson -Value (Get-SafePropertyValue -Object $targetOp -Name 'security') -Depth 20)) 'DATA'
Write-Report ('Global security: ' + (Convert-ToReportJson -Value (Get-SafePropertyValue -Object $spec -Name 'security') -Depth 20)) 'DATA'
Write-Report ('securitySchemes: ' + (Convert-ToReportJson -Value (Get-SafePropertyValue -Object $spec.components -Name 'securitySchemes') -Depth 20)) 'DATA'
Write-Report ('Request body: ' + (Convert-ToReportJson -Value (Get-SafePropertyValue -Object $targetOp -Name 'requestBody') -Depth 20)) 'DATA'
Write-Report ('responses: ' + (Convert-ToReportJson -Value $targetOp.responses -Depth 20)) 'DATA'

# STEP 8 - Discover a valid existing article from safe Article GET operations
Write-Section 'STEP 8 - Automatic read-only Article discovery'
$discoveryOperations = @(Get-ArticleDiscoveryOperations -Spec $spec -ExcludedPath $TargetPath)
Write-Report "Article-tagged GET discovery candidates: $($discoveryOperations.Count)" 'DATA'
foreach ($candidateOp in $discoveryOperations) {
    Write-Report ("Candidate score={0}; path={1}; operationId={2}; summary={3}" -f $candidateOp.Score, $candidateOp.Path, $candidateOp.Operation.operationId, $candidateOp.Summary) 'DATA'
}

$discovered = $null
$selectedDiscovery = $null
$requestCount = 0
foreach ($candidateOp in $discoveryOperations) {
    if ($requestCount -ge $MaxDiscoveryRequests) { break }

    $queryPlan = New-OperationQuery -Operation $candidateOp.Operation -Spec $spec -StoreCode $Store -DiscoveryMode
    if (-not $queryPlan.CanInvoke) {
        Write-Report ("SKIP {0}: {1}" -f $candidateOp.Path, ($queryPlan.Reasons -join '; ')) 'WARN'
        continue
    }

    $queryString = ConvertTo-QueryString -Parameters $queryPlan.Query
    $uri = "$DashboardBaseUrl$($candidateOp.Path)"
    if (-not [string]::IsNullOrWhiteSpace($queryString)) { $uri = "$uri`?$queryString" }

    $requestCount++
    Write-Report "Discovery GET #${requestCount}: $uri" 'DATA'
    $result = Invoke-ReadOnlyGet -Uri $uri -RequestTimeoutSec $TimeoutSec
    Write-Report "Discovery HTTP status: $($result.StatusCode); elapsed=$($result.ElapsedMs)ms" $(if ($result.TransportOk) {'DATA'} else {'WARN'})
    if (-not $result.TransportOk) {
        Write-Report "Discovery transport error: $($result.Error)" 'WARN'
        continue
    }
    if ($result.StatusCode -lt 200 -or $result.StatusCode -ge 300) {
        Write-Report (Get-ResponseClassification -Result $result) 'WARN'
        continue
    }

    $json = Convert-ContentToObject -Content $result.Content
    if ($null -eq $json) {
        Write-Report 'Discovery response was not JSON or was empty.' 'WARN'
        continue
    }

    if ($json -is [System.Array]) {
        Write-Report "Discovery JSON shape: array; count=$($json.Count)" 'DATA'
    }
    else {
        $topLevelNames = @($json.PSObject.Properties.Name)
        Write-Report "Discovery JSON shape: $($json.GetType().FullName); top-level properties=$($topLevelNames -join ', ')" 'DATA'
    }

    $candidate = Find-ArticleCandidate -Node $json
    if ($null -ne $candidate) {
        $discovered = $candidate
        $selectedDiscovery = [pscustomobject]@{
            Path   = $candidateOp.Path
            Uri    = $uri
            Result = $result
        }
        Write-Report "Article candidate discovered from $($candidateOp.Path)." 'PASS'
        Write-Report "Candidate object properties: $($candidate.PropertyNames -join ', ')" 'DATA'
        foreach ($field in $candidate.CandidateFields) {
            Write-Report ("Candidate field: {0}; value preview: {1}" -f $field.Key, (Get-RedactedPreview $field.Value)) 'DATA'
        }
        break
    }
    else {
        Write-Report 'No plausible article identifier field was found in this response.' 'WARN'
        Write-Report ('Response preview: ' + (Get-BodyPreview -Content $result.Content -MaxLength 1500)) 'DATA'
    }
}

# STEP 9 - Execute target GET automatically using discovered article field(s)
Write-Section 'STEP 9 - Automatic target GET /common/articles/content'
$targetResult = $null
$successfulField = $null
if ($null -eq $discovered) {
    Write-Report 'No article could be discovered safely from available GET operations. Target request is not guessed and will not be executed with invented business values.' 'WARN'
}
else {
    foreach ($field in @($discovered.CandidateFields)) {
        $targetQuery = @{
            store = $Store
            key   = $field.Key
            value = [string]$field.Value
        }
        foreach ($k in $pageableQuery.Keys) { $targetQuery[$k] = $pageableQuery[$k] }

        $targetQueryString = ConvertTo-QueryString -Parameters $targetQuery
        $uri = "$TargetUrl`?$targetQueryString"
        Write-Report ("Target GET using key={0}; value preview={1}" -f $field.Key, (Get-RedactedPreview $field.Value)) 'DATA'

        $attempt = Invoke-ReadOnlyGet -Uri $uri -RequestTimeoutSec $TimeoutSec
        Write-Report "HTTP status: $($attempt.StatusCode); elapsed=$($attempt.ElapsedMs)ms" $(if ($attempt.TransportOk) {'DATA'} else {'FAIL'})
        Write-Report (Get-ResponseClassification -Result $attempt) $(if ($attempt.StatusCode -ge 200 -and $attempt.StatusCode -lt 300) {'PASS'} elseif ($attempt.TransportOk) {'WARN'} else {'FAIL'})
        Write-Report ('Response headers: ' + (Convert-ToReportJson -Value (Convert-HeadersForReport -Headers $attempt.Headers) -Depth 10)) 'DATA'
        Write-Report ('Response body preview: ' + (Get-BodyPreview -Content $attempt.Content -MaxLength 4000)) 'DATA'

        $targetResult = $attempt
        if ($attempt.StatusCode -ge 200 -and $attempt.StatusCode -lt 300) {
            $successfulField = $field
            break
        }
    }
}

# STEP 10 - Final classification and summary
Write-Section 'STEP 10 - Final diagnostic summary'
Write-Report "TCP 9001 reachable: $tcpOk" $(if ($tcpOk) {'PASS'} else {'FAIL'})
Write-Report "OpenAPI loaded: $($null -ne $spec)" $(if ($null -ne $spec) {'PASS'} else {'FAIL'})
Write-Report "OpenAPI version: $($spec.openapi)" 'DATA'
Write-Report "AIMS version/build: $($spec.info.version)" 'DATA'
Write-Report "Target route GET $TargetPath exists: $($null -ne $targetGetProperty)" 'PASS'
Write-Report "Selected discovery endpoint: $(if ($null -ne $selectedDiscovery) {$selectedDiscovery.Path} else {'<none>'})" 'DATA'

if ($null -ne $successfulField) {
    Write-Report "Discovered key: $($successfulField.Key)" 'DATA'
    Write-Report "Discovered value preview: $(Get-RedactedPreview $successfulField.Value)" 'DATA'
    Write-Report "Target HTTP status: $($targetResult.StatusCode)" 'PASS'
    Write-Report 'CONCLUSION: remote, non-browser access to this AIMS Article API operation succeeded.' 'PASS'
}
elseif ($null -ne $targetResult) {
    $firstField = @($discovered.CandidateFields)[0]
    Write-Report "Discovered key attempted: $($firstField.Key)" 'DATA'
    Write-Report "Discovered value preview: $(Get-RedactedPreview $firstField.Value)" 'DATA'
    Write-Report "Last target HTTP status: $($targetResult.StatusCode)" 'WARN'
    Write-Report ('CONCLUSION: ' + (Get-ResponseClassification -Result $targetResult)) 'WARN'
}
else {
    Write-Report 'Discovered key: <none>' 'WARN'
    Write-Report 'Discovered value preview: <none>' 'WARN'
    Write-Report 'Target HTTP status: <not executed>' 'WARN'
    Write-Report 'CONCLUSION: network/OpenAPI diagnostics completed, but no safe article value was automatically discoverable.' 'WARN'
}

Write-Report 'No modifying HTTP methods were executed.' 'PASS'
Write-Report "FINAL REPORT: $ReportPath" 'DATA'
Write-Host "`nDiagnostic complete. Upload or paste this report for review:`n$ReportPath`n"
