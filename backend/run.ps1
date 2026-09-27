param(
    [string]$Port = "8000"
)

if (-not (Test-Path ".venv")) {
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}

if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match "^\s*([^#][^=]+)=(.*)$") {
            [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), "Process")
        }
    }
    Write-Host "Loaded .env"
} else {
    Write-Host "No .env found; copy .env.example to .env and fill API keys."
}

.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port $Port --reload