param([Parameter(Mandatory)][string]$RequestFile)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
try {
    $request = Get-Content -Raw -LiteralPath $RequestFile | ConvertFrom-Json -AsHashtable
    $distribution = Join-Path $request.workspace 'Repositories/Distribution'
    $base = $request.base
    . (Join-Path $distribution 'Tools/Distribution.ps1')
    $context = Get-R4DistributionContext $distribution
    $plan = New-R4DistributionPlan $context $base
    $result = New-R4OSInstallationImage -Root $distribution -Profile $base -InputList $plan -ToolsReady
    Copy-R4DistributionLegal $context (Split-Path $result.image -Parent)
    # Import only local helper functions; no preparation/publish entry point runs.
    . (Join-Path $distribution 'Tools/Release.ps1') -Action Library -TechnicalCandidate
    # A local package has no GitHub publication size limit.
    $script:MaximumAssetBytes = [long]::MaxValue
    $releaseContext = Get-ReleaseContext
    $definition = Get-ProfileDefinition $releaseContext $base
    $package = New-ProfilePackage $releaseContext $request.version $definition $request.staging $request.manifest
    $record = @{
        image = $result.image
        image_record = Join-Path (Split-Path $result.image -Parent) 'image.json'
        package = $package
        inventory = Join-Path $context.output 'Generated/MODULES.JSON'
        image_plan = $plan
    }
    [IO.File]::WriteAllText($request.result, ($record | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
