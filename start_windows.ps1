param(
    [string]$Host = "0.0.0.0",
    [int]$Port = 8080,
    [switch]$NoReload
)

$ErrorActionPreference = "Stop"

function Load-DotEnv {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return
    }

    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            return
        }

        $pair = $line -split "=", 2
        if ($pair.Count -lt 2) {
            return
        }

        $key = $pair[0].Trim()
        $value = $pair[1].Trim()

        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        [System.Environment]::SetEnvironmentVariable($key, $value, "Process")
    }
}

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$activateScript = Join-Path $ProjectRoot ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $activateScript)) {
    Write-Error ".venv is missing. Run: py -3.11 -m venv .venv"
    exit 1
}

$envFile = Join-Path $ProjectRoot ".env"
Load-DotEnv -Path $envFile

. $activateScript

if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
    Write-Warning "OPENAI_API_KEY is empty. Set it in .env or current shell."
}

$uvicornArgs = @(
    "app.main:app",
    "--host", $Host,
    "--port", $Port.ToString()
)

if (-not $NoReload) {
    $uvicornArgs += "--reload"
}

python -m uvicorn @uvicornArgs
