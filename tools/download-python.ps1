#Requires -Version 5.1
<#
    Downloads one file with a console progress bar.

    Invoke-WebRequest's own progress bar is not an option here: rendering it
    (via Write-Progress) is a well-known source of a 10-100x download slowdown
    in Windows PowerShell 5.1, which is exactly why the caller used to disable
    it with $ProgressPreference = 'SilentlyContinue' and accept a silent
    download instead. Reading the response stream manually and drawing a
    plain '\r'-updated bar with Write-Host sidesteps that slowdown entirely
    while still giving the console window something to show.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Url,
    [Parameter(Mandatory)] [string]$OutFile,
    [int]$TimeoutSec = 60,
    [string]$LogFile
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

function Log([string]$line) {
    if ($LogFile) {
        try { Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'HH:mm:ss')] $line" -Encoding utf8 } catch {}
    }
}

function DrawBar([long]$done, [long]$total) {
    if ($total -le 0) {
        Write-Host -NoNewline ("`rDownloading Python -- {0:N1} MB so far..." -f ($done / 1MB))
        return
    }
    $pct = [int](($done * 100) / $total)
    $filled = [int]($pct / 4)
    $bar = ('#' * $filled).PadRight(25)
    Write-Host -NoNewline ("`rDownloading Python [{0}] {1,3}%" -f $bar, $pct)
}

$response = $null
$responseStream = $null
$fileStream = $null
try {
    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.Timeout = $TimeoutSec * 1000
    $request.ReadWriteTimeout = $TimeoutSec * 1000
    $request.UserAgent = 'position-size-calculator-launcher'

    $response = $request.GetResponse()
    $total = $response.ContentLength
    $responseStream = $response.GetResponseStream()
    $fileStream = [System.IO.File]::Create($OutFile)

    $buffer = New-Object byte[] 65536
    $downloaded = 0L
    DrawBar $downloaded $total
    while (($read = $responseStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $fileStream.Write($buffer, 0, $read)
        $downloaded += $read
        DrawBar $downloaded $total
    }
    Write-Host ""
    Log "download ok: $downloaded bytes"
    exit 0
} catch {
    Write-Host ""
    Write-Host $_.Exception.Message
    Log "download failed: $($_.Exception.Message)"
    exit 1
} finally {
    if ($fileStream) { $fileStream.Dispose() }
    if ($responseStream) { $responseStream.Dispose() }
    if ($response) { $response.Dispose() }
}
