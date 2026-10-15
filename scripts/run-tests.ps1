Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Run-Step([string]$Title, [scriptblock]$Block) {
  Write-Host ""
  Write-Host "==> $Title" -ForegroundColor Cyan
  & $Block
}

function Run-ComposeTest([string]$ServiceName) {
  Run-Step "Tests: $ServiceName" {
    # Fuerza el entrypoint a python para evitar imágenes con CMD/ENTRYPOINT distintos
    docker compose run --rm --entrypoint python $ServiceName -m pytest -q
  }
}

Run-Step "Starting shared dependencies" {
  docker compose up -d database redis ollama ollama-init
}

Run-Step "Building service images (ensures config files are included)" {
  docker compose build xml-processor rag-service llm-service session-proxy
}

Run-ComposeTest "xml-processor"
Run-ComposeTest "rag-service"
Run-ComposeTest "llm-service"
Run-ComposeTest "session-proxy"

Run-Step "All tests passed" {
  Write-Host "OK" -ForegroundColor Green
}

