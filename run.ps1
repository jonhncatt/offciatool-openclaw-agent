$ErrorActionPreference = "Stop"

$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $rootDir

if (Test-Path ".env") {
  Get-Content ".env" | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#")) {
      if ($line.StartsWith("export ")) {
        $line = $line.Substring(7).Trim()
      }

      $eq = $line.IndexOf("=")
      if ($eq -ge 1) {
        $key = $line.Substring(0, $eq).Trim().TrimStart([char]0xFEFF)
        if ($key) {
          $value = $line.Substring($eq + 1).Trim()
          if (
            ($value.Length -ge 2) -and
            (
              ($value.StartsWith('"') -and $value.EndsWith('"')) -or
              ($value.StartsWith("'") -and $value.EndsWith("'"))
            )
          ) {
            $value = $value.Substring(1, $value.Length - 2)
          }

          if ($value.Contains(" #")) {
            $value = $value.Split(" #", 2)[0].TrimEnd()
          }

          Set-Item -Path "Env:$key" -Value $value
        }
      }
    }
  }
}

if (-not $env:OPENAI_API_KEY) {
  Write-Warning "OPENAI_API_KEY is not set. Server will start, but /api/chat requests will fail until key is configured."
}

$venvPython = Join-Path $rootDir ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
  & $venvPython -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
  exit $LASTEXITCODE
}

py -3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
exit $LASTEXITCODE
