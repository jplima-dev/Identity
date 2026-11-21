$ErrorActionPreference = 'Stop'

$scripts = Join-Path $PSScriptRoot '.venv\Scripts'
$command = Join-Path $scripts 'identity.exe'
if (-not (Test-Path -LiteralPath $command -PathType Leaf)) {
    throw 'Executável ausente. Execute uv sync uma vez dentro da pasta Identity.'
}

$current = [Environment]::GetEnvironmentVariable('Path', 'User')
$entries = @($current -split ';' | Where-Object { $_ })
$entries = @($entries | Where-Object {
    -not $_.TrimEnd('\').EndsWith('\Identity\.venv\Scripts', [StringComparison]::OrdinalIgnoreCase)
})
$updated = ($entries + $scripts) -join ';'
if ($updated -ne $current.TrimEnd(';')) {
    [Environment]::SetEnvironmentVariable('Path', $updated, 'User')
}
$env:Path = "$env:Path;$scripts"
Write-Output "Comando instalado: $command"
Write-Output 'Abra um terminal novo e execute: identity --help'
