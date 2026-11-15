$ErrorActionPreference = 'Stop'

$scripts = Join-Path $PSScriptRoot '.venv\Scripts'
$command = Join-Path $scripts 'identity.exe'
if (-not (Test-Path -LiteralPath $command -PathType Leaf)) {
    throw 'Executável ausente. Execute uv sync uma vez dentro da pasta Identity.'
}

$current = [Environment]::GetEnvironmentVariable('Path', 'User')
$entries = @($current -split ';' | Where-Object { $_ })
if (-not ($entries | Where-Object { $_.TrimEnd('\') -ieq $scripts.TrimEnd('\') })) {
    [Environment]::SetEnvironmentVariable('Path', (($entries + $scripts) -join ';'), 'User')
}
$env:Path = "$env:Path;$scripts"
Write-Output "Comando instalado: $command"
Write-Output 'Abra um terminal novo e execute: identity --help'
