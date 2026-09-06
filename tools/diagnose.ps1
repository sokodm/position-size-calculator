#Requires -Version 5.1
<#
    Diagnostics for the Position Size Calculator's Windows launcher.

    Probes the machine for every condition the launcher depends on, runs the
    launcher as a supervised child process, captures everything it prints, and
    writes one self-contained HTML report.

    Design constraint: the launcher failing is the EXPECTED case here, and it
    may be killed outright rather than exiting. So the launcher runs as a child
    -- this script survives its death -- and every probe is guarded
    individually, because a report missing one row is still useful while a
    report that never got written is not.
#>
[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 600,
    [switch]$SkipLaunch
)

Set-StrictMode -Off
$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

$root   = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root 'logs'
$stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
$report = Join-Path $logDir "diagnostics-$stamp.html"
$outLog = Join-Path $logDir "launcher-$stamp.log"

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

function HtmlEscape([string]$s) {
    if ($null -eq $s) { return '' }
    return $s.Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;')
}

$facts = New-Object System.Collections.ArrayList

function Add-Fact {
    param([string]$Section, [string]$Name, [scriptblock]$Probe, [string]$Expected = '')
    $value = ''
    $ok = $true
    # A probe must fail INTO the report, never onto the console the report
    # exists to replace. Under 'Continue' a non-terminating error does exactly
    # that -- it prints, skips the catch, and leaves the row silently empty --
    # so probes run under 'Stop' to make every failure catchable.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Stop'
    try {
        $value = & $Probe
    } catch {
        $value = "probe failed: $($_.Exception.Message)"
        $ok = $false
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($null -eq $value -or "$value" -eq '') { $value = '(none)' }
    if ($value -is [array]) { $value = ($value -join "`n") }
    [void]$facts.Add([pscustomobject]@{
        Section  = $Section
        Name     = $Name
        Value    = [string]$value
        Ok       = $ok
        Expected = $Expected
    })
}

Write-Host ''
Write-Host '  Collecting diagnostics ...'
Write-Host ''

# ---------------------------------------------------------------- environment
Add-Fact 'Machine' 'Windows version' {
    (Get-CimInstance Win32_OperatingSystem).Caption + ' (build ' +
        [System.Environment]::OSVersion.Version.Build + ')'
}
Add-Fact 'Machine' 'Architecture'      { $env:PROCESSOR_ARCHITECTURE }
Add-Fact 'Machine' 'PowerShell'        { $PSVersionTable.PSVersion.ToString() }
Add-Fact 'Machine' 'Execution policy'  { (Get-ExecutionPolicy -Scope Process).ToString() + ' (process), ' + (Get-ExecutionPolicy).ToString() + ' (effective)' }
Add-Fact 'Machine' 'User is admin' {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# The launcher unpacks with tar and downloads with Invoke-WebRequest. Both are
# present on a healthy Windows 10 1803+, so an absence here IS the diagnosis.
Add-Fact 'Launcher prerequisites' 'tar available' {
    $t = Get-Command tar -ErrorAction SilentlyContinue
    if ($t) { $t.Source } else { 'NOT FOUND -- launcher cannot unpack its Python' }
} 'a path'
Add-Fact 'Launcher prerequisites' 'curl available' {
    $c = Get-Command curl -ErrorAction SilentlyContinue
    if ($c) { $c.Source } else { '(none)' }
}

# --------------------------------------------------------------------- python
Add-Fact 'Python' 'py launcher' {
    $p = Get-Command py -ErrorAction SilentlyContinue
    if (-not $p) { return 'NOT FOUND' }
    $v = & py -3 --version 2>&1
    "$($p.Source) -> $v"
}
Add-Fact 'Python' 'python on PATH' {
    $p = Get-Command python -ErrorAction SilentlyContinue
    if (-not $p) { return 'NOT FOUND' }
    $v = & python --version 2>&1
    "$($p.Source) -> $v"
}
Add-Fact 'Python' 'downloaded runtime (.runtime)' {
    $rt = Join-Path $root '.runtime\python\python.exe'
    if (Test-Path $rt) { "$rt -> " + (& $rt --version 2>&1) } else { 'not present' }
}
Add-Fact 'Python' 'virtual environment (.venv)' {
    $ve = Join-Path $root '.venv\Scripts\python.exe'
    if (Test-Path $ve) { "$ve -> " + (& $ve --version 2>&1) } else { 'not present' }
}

# ----------------------------------------------------------------- the folder
Add-Fact 'Folder' 'App folder' { $root }
Add-Fact 'Folder' 'Path contains spaces' { $root -match ' ' }
Add-Fact 'Folder' 'Running from a temp/zip preview' {
    # Double-clicking a .bat inside a ZIP preview copies it somewhere under
    # Temp and runs it there, with none of its sibling files -- a very common
    # cause of "the window closed and nothing happened".
    $t = [IO.Path]::GetTempPath()
    if ($root.StartsWith($t, 'OrdinalIgnoreCase')) {
        'YES -- the app was not properly extracted'
    } else { 'no' }
} 'no'
Add-Fact 'Folder' 'Files present' {
    $needed = @('run.py', 'app.py', 'requirements.txt', 'Start Calculator (Windows).bat')
    $lines = @()
    foreach ($f in $needed) {
        $present = Test-Path (Join-Path $root $f)
        $mark = 'MISSING'
        if ($present) { $mark = 'ok' }
        $lines += ("{0,-32} {1}" -f $f, $mark)
    }
    $lines
}
Add-Fact 'Folder' 'Free disk space' {
    $d = Get-PSDrive -Name ((Split-Path -Qualifier $root).TrimEnd(':')) -ErrorAction SilentlyContinue
    if ($d) { '{0:N1} GB free' -f ($d.Free / 1GB) } else { 'unknown' }
}
Add-Fact 'Folder' 'Blocked by Mark of the Web' {
    # Files from a downloaded ZIP carry a zone identifier; some policies refuse
    # to execute them, which can kill the window with no message at all.
    $bat = Join-Path $root 'Start Calculator (Windows).bat'
    $z = Get-Item -Path $bat -Stream Zone.Identifier -ErrorAction SilentlyContinue
    if ($z) { 'YES -- right-click the file, Properties, tick Unblock' } else { 'no' }
} 'no'

# ---------------------------------------------------------------------- network
Add-Fact 'Network' 'Can reach GitHub releases' {
    try {
        $r = Invoke-WebRequest -Uri 'https://github.com' -UseBasicParsing `
                -Method Head -TimeoutSec 15
        "HTTP $($r.StatusCode)"
    } catch {
        "FAILED -- $($_.Exception.Message)"
    }
}
Add-Fact 'Network' 'TLS interception (proxy certificate)' {
    try {
        $req = [Net.HttpWebRequest]::Create('https://github.com')
        $req.Method = 'HEAD'
        $req.Timeout = 15000
        $resp = $req.GetResponse()
        $cert = $req.ServicePoint.Certificate
        $resp.Close()
        if ($cert) { 'issuer: ' + $cert.Issuer } else { 'unknown' }
    } catch {
        "could not inspect -- $($_.Exception.Message)"
    }
}

# ------------------------------------------------------- run the launcher
$launchOutput = ''
$launchExit   = $null
$launchNote   = ''

if (-not $SkipLaunch) {
    $bat = Join-Path $root 'Start Calculator (Windows).bat'
    if (-not (Test-Path $bat)) {
        $launchNote = 'The launcher file is missing from the folder.'
    } else {
        Write-Host "  Running the launcher (up to $TimeoutSeconds s) ..."
        Write-Host ''
        # PSC_DIAGNOSE makes run.py stop once it has proved the app serves,
        # instead of blocking forever the way a normal start does.
        $env:PSC_DIAGNOSE = '1'
        # Redirect inside cmd rather than via Start-Process: it keeps the
        # quoting sane for a path containing spaces and parentheses.
        $cmdline = '/c ""{0}" > "{1}" 2>&1 < NUL"' -f $bat, $outLog
        try {
            $proc = Start-Process -FilePath $env:ComSpec -ArgumentList $cmdline `
                        -WorkingDirectory $root -PassThru -WindowStyle Hidden
            if ($proc.WaitForExit($TimeoutSeconds * 1000)) {
                $launchExit = $proc.ExitCode
            } else {
                $launchNote = "The launcher was still running after $TimeoutSeconds s and was stopped."
                try { $proc.Kill() } catch { }
            }
        } catch {
            $launchNote = "Could not start the launcher: $($_.Exception.Message)"
        }
        Remove-Item Env:\PSC_DIAGNOSE -ErrorAction SilentlyContinue
        if (Test-Path $outLog) {
            $launchOutput = Get-Content -Raw -Path $outLog -ErrorAction SilentlyContinue
        }
        if ($null -eq $launchOutput) { $launchOutput = '' }
        if ($launchOutput.Trim() -eq '' -and $launchNote -eq '') {
            $launchNote = 'The launcher produced no output at all, which usually means the process was terminated by something outside it (antivirus or endpoint protection are the usual culprits).'
        }
    }
} else {
    $launchNote = 'Skipped at the caller''s request.'
}

# ----------------------------------------------------- pull exceptions out
$exceptions = New-Object System.Collections.ArrayList

function Collect-Exceptions([string]$text, [string]$origin) {
    if ([string]::IsNullOrWhiteSpace($text)) { return }
    $lines = $text -split "`r?`n"
    $i = 0
    while ($i -lt $lines.Count) {
        $line = $lines[$i]
        if ($line -match 'Traceback \(most recent call last\)') {
            # A traceback runs until the first line that is neither indented
            # nor blank -- that final line is the exception itself.
            $block = @($line)
            $i++
            while ($i -lt $lines.Count) {
                $block += $lines[$i]
                if ($lines[$i] -match '^\S' -and $lines[$i] -notmatch '^\s*$') { break }
                $i++
            }
            [void]$exceptions.Add([pscustomobject]@{
                Origin = $origin
                Title  = ($block | Where-Object { $_ -match '^\S' } | Select-Object -Last 1)
                Detail = ($block -join "`n")
            })
        }
        elseif ($line -match '^\s*ERROR:\s*(.+)$') {
            [void]$exceptions.Add([pscustomobject]@{
                Origin = $origin
                Title  = $Matches[1].Trim()
                Detail = $line
            })
        }
        elseif ($line -match 'is not recognized as an internal or external command' -or
                $line -match 'The system cannot find' -or
                $line -match 'Access is denied' -or
                $line -match 'was unexpected at this time') {
            [void]$exceptions.Add([pscustomobject]@{
                Origin = $origin
                Title  = $line.Trim()
                Detail = $line
            })
        }
        $i++
    }
}

Collect-Exceptions $launchOutput 'launcher console'

# run.py writes its own traceback here, which survives even when the console
# window is destroyed before anyone can read it.
$pyLog = Join-Path $logDir 'run-py-last-error.log'
$pyLogText = ''
if (Test-Path $pyLog) {
    $pyLogText = Get-Content -Raw -Path $pyLog -ErrorAction SilentlyContinue
    Collect-Exceptions $pyLogText 'run.py'
}

# ------------------------------------------------------------------ verdict
$verdict = 'Inconclusive -- send this report on.'
$verdictClass = 'warn'
if ($launchOutput -match 'is running at http') {
    $verdict = 'The app started successfully.'
    $verdictClass = 'ok'
} elseif ($launchNote -match 'terminated by something outside it') {
    $verdict = 'The launcher was killed before it could print anything. Suspect antivirus or endpoint protection.'
    $verdictClass = 'bad'
} elseif ($exceptions.Count -gt 0) {
    $verdict = "The launcher failed with $($exceptions.Count) error(s) -- see Exceptions below."
    $verdictClass = 'bad'
} elseif ($launchExit -ne $null -and $launchExit -ne 0) {
    $verdict = "The launcher exited with code $launchExit."
    $verdictClass = 'bad'
}

# ------------------------------------------------------------------- report
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine('<!doctype html><html lang="en"><head><meta charset="utf-8">')
[void]$sb.AppendLine('<meta name="viewport" content="width=device-width,initial-scale=1">')
[void]$sb.AppendLine('<title>Position Size Calculator - Windows diagnostics</title><style>')
[void]$sb.AppendLine(@'
:root{--bg:#f7f7f8;--fg:#1c1c1e;--card:#fff;--line:#e3e3e6;--mut:#6b6b70;
--ok:#0f7b3d;--okbg:#e8f6ed;--bad:#b3261e;--badbg:#fdeceb;--warn:#8a6100;--warnbg:#fdf3e0;}
@media(prefers-color-scheme:dark){:root{--bg:#161618;--fg:#ececef;--card:#1f1f22;
--line:#333338;--mut:#a0a0a8;--ok:#5fd08a;--okbg:#122e1e;--bad:#ff8a80;--badbg:#3a1a18;
--warn:#f0c674;--warnbg:#33280f;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;padding:24px}
.wrap{max-width:1000px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 10px;
text-transform:uppercase;letter-spacing:.06em;color:var(--mut)}
.sub{color:var(--mut);margin:0 0 20px}
.verdict{padding:14px 16px;border-radius:8px;font-weight:600;margin:0 0 8px}
.ok{background:var(--okbg);color:var(--ok)}.bad{background:var(--badbg);color:var(--bad)}
.warn{background:var(--warnbg);color:var(--warn)}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:8px;overflow:hidden}
td{padding:8px 12px;border-top:1px solid var(--line);vertical-align:top}
tr:first-child td{border-top:0}
td.k{width:230px;color:var(--mut);white-space:nowrap}
td.v{font-family:ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
.flag{color:var(--bad);font-weight:700}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:14px;overflow-x:auto;font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.exc{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--bad);
border-radius:8px;padding:12px 14px;margin:0 0 12px}
.exc h3{margin:0 0 6px;font-size:14px;color:var(--bad);font-family:ui-monospace,Consolas,monospace}
.exc .org{color:var(--mut);font-size:12px;margin:0 0 8px}
.exc pre{margin:0;background:transparent;border:0;padding:0}
.none{color:var(--mut);font-style:italic}
'@)
[void]$sb.AppendLine('</style></head><body><div class="wrap">')
[void]$sb.AppendLine('<h1>Position Size Calculator &mdash; Windows diagnostics</h1>')
[void]$sb.AppendLine('<p class="sub">' + (HtmlEscape (Get-Date -Format 'dddd d MMMM yyyy, HH:mm:ss')) + ' &middot; ' + (HtmlEscape $root) + '</p>')
[void]$sb.AppendLine('<div class="verdict ' + $verdictClass + '">' + (HtmlEscape $verdict) + '</div>')
if ($launchNote -ne '') {
    [void]$sb.AppendLine('<p class="sub">' + (HtmlEscape $launchNote) + '</p>')
}

[void]$sb.AppendLine('<h2>Exceptions</h2>')
if ($exceptions.Count -eq 0) {
    [void]$sb.AppendLine('<p class="none">No exceptions or error messages were captured.</p>')
} else {
    foreach ($e in $exceptions) {
        [void]$sb.AppendLine('<div class="exc"><h3>' + (HtmlEscape $e.Title) + '</h3>')
        [void]$sb.AppendLine('<p class="org">from ' + (HtmlEscape $e.Origin) + '</p>')
        [void]$sb.AppendLine('<pre>' + (HtmlEscape $e.Detail) + '</pre></div>')
    }
}

$sections = $facts | Select-Object -ExpandProperty Section -Unique
foreach ($s in $sections) {
    [void]$sb.AppendLine('<h2>' + (HtmlEscape $s) + '</h2><table>')
    foreach ($f in ($facts | Where-Object { $_.Section -eq $s })) {
        $cls = 'v'
        if (-not $f.Ok -or $f.Value -match 'NOT FOUND|MISSING|FAILED|^YES') { $cls = 'v flag' }
        [void]$sb.AppendLine('<tr><td class="k">' + (HtmlEscape $f.Name) + '</td><td class="' +
            $cls + '">' + (HtmlEscape $f.Value) + '</td></tr>')
    }
    [void]$sb.AppendLine('</table>')
}

[void]$sb.AppendLine('<h2>Launcher transcript</h2>')
if ([string]::IsNullOrWhiteSpace($launchOutput)) {
    [void]$sb.AppendLine('<p class="none">The launcher printed nothing.</p>')
} else {
    [void]$sb.AppendLine('<pre>' + (HtmlEscape $launchOutput) + '</pre>')
}

if (-not [string]::IsNullOrWhiteSpace($pyLogText)) {
    [void]$sb.AppendLine('<h2>run.py error log</h2><pre>' + (HtmlEscape $pyLogText) + '</pre>')
}

[void]$sb.AppendLine('</div></body></html>')

[IO.File]::WriteAllText($report, $sb.ToString(), (New-Object Text.UTF8Encoding($false)))

Write-Host ''
Write-Host "  Verdict: $verdict"
Write-Host "  Report:  $report"
Write-Host ''
try { Start-Process $report } catch { }
