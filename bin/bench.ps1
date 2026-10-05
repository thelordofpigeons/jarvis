<#
Phase 1 benchmark wrapper (report section 2).

Only llama-bench measures real prompt-processing and generation throughput on this
card. llmfit and friends estimate. So this wraps llama-bench and records, per model,
prompt-processing tok/s, generation tok/s, load time and the largest context that
loads without falling over.

Results append to logs/bench.jsonl so the phase 1 agent-model decision
(gpt-oss-20b against qwen3-30b-a3b) is made from measurements, not model cards.

Exit codes
  0  benchmark completed
  4  llama-bench not found, nothing measured (phase 1 prerequisite missing)
  5  model file not found
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)] [string] $ModelPath,
  [string] $Label = '',
  [int[]]  $ContextSizes = @(4096, 16384, 32768),
  [int]    $PromptTokens = 512,
  [int]    $GenTokens = 128,
  [int]    $GpuLayers = 999,
  [string] $LlamaBench = ''
)

$ErrorActionPreference = 'Stop'
$root    = Split-Path -Parent $PSScriptRoot
$logPath = Join-Path $root 'logs\bench.jsonl'

function Resolve-LlamaBench {
  if ($LlamaBench -and (Test-Path $LlamaBench)) { return (Resolve-Path $LlamaBench).Path }
  $cmd = Get-Command 'llama-bench' -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  foreach ($c in @(
      (Join-Path $root 'vendor\llama.cpp\llama-bench.exe'),
      "$env:LOCALAPPDATA\llama.cpp\llama-bench.exe",
      "$HOME\llama.cpp\build\bin\Release\llama-bench.exe"
    )) { if (Test-Path $c) { return $c } }
  return $null
}

$bench = Resolve-LlamaBench
if (-not $bench) {
  Write-Host 'llama-bench not found. Phase 1 has not installed llama.cpp yet.'
  Write-Host 'Expected on PATH, or under vendor\llama.cpp\, or pass -LlamaBench <path>.'
  exit 4
}
if (-not (Test-Path $ModelPath)) {
  Write-Host "model not found: $ModelPath"
  exit 5
}

if (-not $Label) { $Label = [System.IO.Path]::GetFileNameWithoutExtension($ModelPath) }
$modelSizeGb = [math]::Round((Get-Item $ModelPath).Length / 1GB, 2)
$gpu = (Get-CimInstance Win32_VideoController | Select-Object -First 1)

Write-Host ("benchmarking {0} ({1} GB) with {2}" -f $Label, $modelSizeGb, $bench)
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPath) | Out-Null

foreach ($ctx in $ContextSizes) {
  Write-Host ("`n-- context {0} --" -f $ctx)
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  $args = @(
    '-m', $ModelPath,
    '-p', $PromptTokens,
    '-n', $GenTokens,
    '-c', $ctx,
    '-ngl', $GpuLayers,
    '-o', 'json'
  )
  $raw = & $bench @args 2>&1
  $sw.Stop()
  $text = ($raw | Out-String)

  $pp = $null; $tg = $null; $status = 'ok'
  try {
    $parsed = $text | ConvertFrom-Json
    foreach ($row in @($parsed)) {
      if ($row.n_prompt -gt 0 -and $row.n_gen -eq 0) { $pp = [double]$row.avg_ts }
      if ($row.n_gen    -gt 0 -and $row.n_prompt -eq 0) { $tg = [double]$row.avg_ts }
    }
    if ($null -eq $pp -and $null -eq $tg) { $status = 'parsed-but-empty' }
  } catch {
    $status = 'failed'
  }

  # A DeviceLost or TDR at a given context is the finding, not an error to hide.
  if ($text -match 'DeviceLost|device lost|VK_ERROR|out of memory|failed to allocate') {
    $status = 'device-error'
  }

  $entry = [ordered]@{
    ts              = (Get-Date).ToUniversalTime().ToString('o')
    event           = 'bench'
    label           = $Label
    model_path      = $ModelPath
    model_size_gb   = $modelSizeGb
    context         = $ctx
    prompt_tokens   = $PromptTokens
    gen_tokens      = $GenTokens
    gpu_layers      = $GpuLayers
    pp_tok_s        = $pp
    tg_tok_s        = $tg
    wall_seconds    = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    status          = $status
    gpu             = $gpu.Name
    driver          = $gpu.DriverVersion
    backend         = 'vulkan'
  }
  # No BOM: see the note in kill-switch.ps1. Keeps bench.jsonl machine-readable.
  [System.IO.File]::AppendAllText(
    $logPath,
    ($entry | ConvertTo-Json -Depth 4 -Compress) + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false))

  Write-Host ("status={0} pp={1} tg={2} wall={3}s" -f $status, $pp, $tg, $entry.wall_seconds)
  if ($status -eq 'device-error') {
    Write-Host 'device error at this context. Higher contexts are not attempted.'
    break
  }
}

Write-Host "`nresults: $logPath"
exit 0
