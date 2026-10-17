param(
    [ValidateSet('demo', 'paper')][string]$Mode = 'demo',
    [string]$Destination = ''
)
$ErrorActionPreference = 'Stop'
if (-not $Destination) { $Destination = [Environment]::GetFolderPath('Desktop') }
if (-not (Test-Path -LiteralPath $Destination -PathType Container)) {
    throw 'The desktop folder was not found.'
}
$launcher = Join-Path $PSScriptRoot 'Launch Bot.cmd'
if (-not (Test-Path -LiteralPath $launcher)) { throw 'Keep this installer in the bot folder.' }
$label = if ($Mode -eq 'paper') { 'Kalshi Bot (Paper)' } else { 'Kalshi Bot (Demo)' }
$path = Join-Path $Destination ($label + '.lnk')
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($path)
if ((Test-Path -LiteralPath $path) -and $shortcut.TargetPath -ne $launcher) {
    throw 'A shortcut with this name already points to another folder. Rename it before installing this one.'
}
$shortcut.TargetPath = $launcher
$shortcut.Arguments = '--mode ' + $Mode
if ($Mode -eq 'paper') { $shortcut.Arguments += ' --config config.example.toml' }
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = 'Open the local Kalshi PAPER bot and dashboard (' + $Mode + ')'
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
$shortcut.Save()
Write-Host "Created: $path"
