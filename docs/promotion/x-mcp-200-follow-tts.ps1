param(
  [string]$Story = "$PSScriptRoot\x-mcp-200-follow-story.json",
  [string]$Out = "$PSScriptRoot\x-mcp-200-follow-work"
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$data = Get-Content $Story -Raw -Encoding utf8 | ConvertFrom-Json
New-Item -ItemType Directory -Force $Out | Out-Null
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice = if ($null -ne $data.voice -and [string]$data.voice) { [string]$data.voice } else { 'Microsoft Huihui Desktop' }
$s.SelectVoice($voice)
$s.Rate = if ($null -ne $data.rate) { [int]$data.rate } else { 0 }
$rows = @()
$index = 0
foreach ($slide in $data.slides) {
  $index++
  $id = ('s{0:D2}' -f $index)
  $path = Join-Path $Out "$id.wav"
  $s.SetOutputToWaveFile($path)
  $s.Speak([string]$slide.narration)
  $s.SetOutputToNull()
  $r = New-Object System.IO.BinaryReader([IO.File]::OpenRead($path))
  $r.BaseStream.Seek(28, 'Begin') | Out-Null
  $bytesPerSecond = $r.ReadInt32()
  $length = $r.BaseStream.Length - 44
  $r.Close()
  $rows += [pscustomobject]@{
    id = $id
    file = $path
    seconds = [math]::Round($length / $bytesPerSecond, 3)
  }
}
$s.Dispose()
$rows | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $Out 'durations.json')
$rows | Format-Table -AutoSize
Write-Output "voice=$voice"

