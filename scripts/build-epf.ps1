# Собирает dist\MCPLocalBridge.epf из XML-исходников каталога epf.
# Сборке нужна лицензия 1С, поэтому конфигуратор подключается к серверной базе (лучше тестовой или
# копии): обработка в базу не загружается, база не меняется. Окно конфигуратора открывается свёрнутым.
#
# Пример: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-epf.ps1 -Server srv1c -Ref TestBase
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Ref,
    [string]$V8Path = '',
    [string]$Output = ''
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Output) { $Output = Join-Path $root 'dist\MCPLocalBridge.epf' }
if (-not $V8Path) {
    # Платформа 8.3 новейшей версии: конфигуратор другой линейки к серверу 8.3 не подключится
    $platform = Get-ChildItem 'C:\Program Files\1cv8' -Directory |
        Where-Object { $_.Name -match '^8\.3\.\d+\.\d+$' } |
        Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
    if (-not $platform) { throw 'Не найдена платформа 8.3 в C:\Program Files\1cv8, укажите -V8Path' }
    $V8Path = Join-Path $platform.FullName 'bin\1cv8.exe'
}

# Конфигуратор читает исходники с локального диска: с сетевого (UNC) пути пакетный режим работает ненадёжно
# TEMP бывает с короткими именами 8.3 (IVANOV~1.PET), Remove-Item на таком пути падает - берём полный
$work = Join-Path (Get-Item -LiteralPath $env:TEMP).FullName 'mcp-1c-local-bridge-build'
if (Test-Path $work) { Remove-Item $work -Recurse -Force }
New-Item -ItemType Directory -Path $work | Out-Null
Copy-Item (Join-Path $root 'epf') (Join-Path $work 'src') -Recurse
$epf = Join-Path $work 'MCPLocalBridge.epf'
$log = Join-Path $work 'build.log'

$arguments = @(
    'DESIGNER',
    "/S`"$Server\$Ref`"",
    '/DisableStartupDialogs',
    '/DisableStartupMessages',
    '/LoadExternalDataProcessorOrReportFromFiles',
    "`"$(Join-Path $work 'src\MCPLocalBridge.xml')`"",
    "`"$epf`"",
    '/Out', "`"$log`""
)
$process = Start-Process -FilePath $V8Path -ArgumentList $arguments -WindowStyle Minimized -Wait -PassThru
$text = if (Test-Path $log) { Get-Content $log -Raw -Encoding UTF8 } else { '' }
if ($process.ExitCode -ne 0 -or -not (Test-Path $epf)) {
    Write-Output $text
    throw "Сборка не удалась, код возврата $($process.ExitCode)"
}
Copy-Item $epf $Output -Force
Remove-Item $work -Recurse -Force
Write-Output "OK: $Output"
