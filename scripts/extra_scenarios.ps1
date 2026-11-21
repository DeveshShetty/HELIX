# Failure scenarios not covered by demo.py: KB erroring, Tickets hung, Summariser down,
# Ollama down, concurrent roster edits. Run with the stack up:
#   powershell -File scripts/extra_scenarios.ps1
function J($m, $u, $b) { Invoke-RestMethod -Method $m $u -ContentType 'application/json' -Body ($b | ConvertTo-Json) -TimeoutSec 60 }
function Code($m, $u, $b) { try { Invoke-WebRequest -Method $m $u -ContentType 'application/json' -Body ($b | ConvertTo-Json) -UseBasicParsing -TimeoutSec 60 | Out-Null; 200 } catch { [int]$_.Exception.Response.StatusCode } }
function W($p) { $n = 0; do { Start-Sleep 2; $n++; $ok = try { Invoke-RestMethod "http://localhost:$p/health" -TimeoutSec 3 } catch { $null } } until ($ok -or $n -gt 60) }
function Ask($q) { $t = Get-Date; $r = J Post http://localhost:8001/chat @{message = $q }; "    {0,-9}{1,-22} degraded={2,-5} ticket={3,-9} {4,5} ms" -f $r.decision, $r.reason, $r.degraded, $(if ($r.ticket_id) { $r.ticket_id } elseif ($r.escalation_pending) { "queued" } else { "-" }), [math]::Round(((Get-Date) - $t).TotalMilliseconds) }

"=== A. Knowledge returning errors (error_rate 0.5)"
J Post http://localhost:8004/admin/fault @{latency_ms = 0; error_rate = 0.5 } | Out-Null
1..6 | ForEach-Object { Ask "how do I reset my password" }
J Post http://localhost:8004/admin/fault @{latency_ms = 0; error_rate = 0.0 } | Out-Null
"    breaker: " + (Invoke-RestMethod http://localhost:8001/config).kb_breaker
Start-Sleep 16; Ask "how do I reset my password"; "    breaker: " + (Invoke-RestMethod http://localhost:8001/config).kb_breaker

"=== B. Tickets hung (docker compose pause tickets)"
docker compose pause tickets 2>&1 | Out-Null
Ask "does helix support the orbit-$(Get-Random -Maximum 99999) protocol"
Start-Sleep 5; docker compose unpause tickets 2>&1 | Out-Null; $t = Get-Date
do { Start-Sleep 2 } until ((Invoke-RestMethod http://localhost:8001/config).escalations_pending -eq 0 -or ((Get-Date) - $t).TotalSeconds -gt 90)
"    after unpause: escalations_pending=$((Invoke-RestMethod http://localhost:8001/config).escalations_pending) after $([math]::Round(((Get-Date) - $t).TotalSeconds)) s"

"=== C. Summariser down while a conversation closes"
docker compose stop summariser 2>&1 | Out-Null
Get-Content scripts\publish_closed_conversation.py | docker compose exec -T chat python - 2>&1 | Select-String "conversation=" | ForEach-Object { "    $_" }
Start-Sleep 3; docker compose exec -T rabbitmq rabbitmqctl list_queues name messages --quiet 2>&1 | Select-String "summariser" | ForEach-Object { "    queue while down: $_" }
"    on-call console /summaries while down: HTTP " + (Code Get http://localhost:8003/summaries $null)
docker compose start summariser 2>&1 | Out-Null; W 8005; Start-Sleep 5
docker compose exec -T rabbitmq rabbitmqctl list_queues name messages --quiet 2>&1 | Select-String "summariser" | ForEach-Object { "    queue after start: $_" }
"    newest draft: " + ((Invoke-RestMethod http://localhost:8005/drafts | Select-Object -First 1) | ForEach-Object { "$($_.draft_id) $($_.ticket_id) $($_.method)" })

"=== D. Ollama down (LLM_MODE=ollama, OLLAMA_URL pointing at a closed port)"
docker compose run -d --no-deps --name chat-ollama-down -e LLM_MODE=ollama -e OLLAMA_URL=http://127.0.0.1:9 -p 8011:8001 chat 2>&1 | Out-Null
$n = 0; do { Start-Sleep 2; $n++; $ok = try { Invoke-RestMethod http://localhost:8011/health -TimeoutSec 3 } catch { $null } } until ($ok -or $n -gt 45)
$t = Get-Date; $r = J Post http://localhost:8011/chat @{message = "how do I reset my password" }
"    {0}/{1} degraded={2} model={3} {4} ms" -f $r.decision, $r.reason, $r.degraded, $r.model, [math]::Round(((Get-Date) - $t).TotalMilliseconds)
"    answer starts: " + $r.answer.Substring(0, [Math]::Min(90, $r.answer.Length))
docker logs chat-ollama-down 2>&1 | Select-String "ollama failed" | Select-Object -First 1 | ForEach-Object { "    log: " + "$_".Substring("$_".IndexOf("[llm]")) }
docker rm -f chat-ollama-down 2>&1 | Out-Null; $env:LLM_MODE = 'simulated'

"=== E. Two admins edit the roster at once (optimistic concurrency)"
$r = Invoke-RestMethod http://localhost:8003/roster; "    roster version $($r.version): $($r.chain -join ' -> ')"
"    admin 1 PUT with version $($r.version): HTTP " + (Code Put http://localhost:8003/roster @{version = $r.version; chain = @("bob", "alice", "carol"); updated_by = "admin1" })
"    admin 2 PUT with version $($r.version) (stale): HTTP " + (Code Put http://localhost:8003/roster @{version = $r.version; chain = @("carol", "bob"); updated_by = "admin2" })
$r2 = Invoke-RestMethod http://localhost:8003/roster; "    roster now version $($r2.version): $($r2.chain -join ' -> ') (by $($r2.updated_by))"
Code Put http://localhost:8003/roster @{version = $r2.version; chain = $r.chain; updated_by = "restore" } | Out-Null
