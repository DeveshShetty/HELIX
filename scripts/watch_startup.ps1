# Start a clean stack (separate compose project, fresh volumes) and print each change in
# container health and in Knowledge's /health status code, until everything is healthy.
#   powershell -File scripts/watch_startup.ps1 [-Project helixclean]
param([string]$Project = "helixclean", [int]$TimeoutSeconds = 600)

$t0 = Get-Date
$job = Start-Job { param($p, $dir) Set-Location $dir; docker compose -p $p up --build -d 2>&1 } -ArgumentList $Project, (Get-Location)
$prev = ""
while (((Get-Date) - $t0).TotalSeconds -lt $TimeoutSeconds) {
    $svc = (docker compose -p $Project ps -a --format "{{.Service}}={{.Status}}" 2>$null |
            Where-Object { $_ -match '^(rabbitmq|chat|tickets|oncall|knowledge|summariser)=' } |
            ForEach-Object { $_ -replace 'Up [^(]*', 'up' }) -join ' '
    $kb = try { Invoke-WebRequest http://localhost:8004/health -UseBasicParsing -TimeoutSec 2 | Out-Null; "kb/health=200" }
          catch { if ($_.Exception.Response) { "kb/health=" + [int]$_.Exception.Response.StatusCode } else { "kb/health=no-connection" } }
    $line = "$svc | $kb"
    if ($line -ne $prev) { "t+{0,4}s  {1}" -f [math]::Round(((Get-Date) - $t0).TotalSeconds), $line; $prev = $line }
    $notReady = ($svc -split ' ' | Where-Object { $_ -and $_ -notmatch '\(healthy\)' }).Count
    if ($svc -and $notReady -eq 0 -and $kb -eq "kb/health=200") { "all healthy"; break }
    Start-Sleep -Milliseconds 700
}
Receive-Job $job -Wait | Select-Object -Last 3
