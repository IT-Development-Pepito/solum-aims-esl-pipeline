# Load environment variables from the .env file into 
# the current PowerShell process only during development
Get-Content .env | ForEach-Object { 
    $line = $_.Trim(); 
    if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) { 
        $name, $value = $line.Split('=', 2); 
        [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process') 
    } 
}