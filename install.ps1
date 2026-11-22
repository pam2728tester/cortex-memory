# Installs Cortex Memory hooks into ~/.claude/settings.json without clobbering
# existing hook entries. Backs up settings.json before writing.

$PythonExe = "C:\Users\Testing\AppData\Local\Programs\Python\Python312\python.exe"
$HooksDir = Join-Path $PSScriptRoot "cortex_memory\hooks"
$SettingsPath = "$env:USERPROFILE\.claude\settings.json"

if (-not (Test-Path $SettingsPath)) {
    throw "settings.json not found at $SettingsPath"
}

$backup = "$SettingsPath.bak-pre-cortex-memory-$(Get-Date -Format 'yyyyMMddHHmmss')"
Copy-Item $SettingsPath $backup
Write-Host "Backed up settings.json to $backup"

$settings = Get-Content $SettingsPath -Raw | ConvertFrom-Json -AsHashtable
if (-not $settings.ContainsKey("hooks")) { $settings["hooks"] = @{} }

function Add-Hook($eventName, $scriptName) {
    $cmd = "`"$PythonExe`" `"$HooksDir\$scriptName`""
    if (-not $settings["hooks"].ContainsKey($eventName)) {
        $settings["hooks"][$eventName] = @()
    }
    $already = $settings["hooks"][$eventName] | Where-Object {
        ($_.hooks | Where-Object { $_.command -like "*$scriptName*" }).Count -gt 0
    }
    if ($already) {
        Write-Host "$eventName already wired to $scriptName, skipping"
        return
    }
    $settings["hooks"][$eventName] += @{ hooks = @(@{ type = "command"; command = $cmd; timeout = 10 }) }
    Write-Host "Added $eventName -> $scriptName"
}

Add-Hook "SessionStart" "session_start.py"
Add-Hook "UserPromptSubmit" "user_prompt_submit.py"
Add-Hook "PostToolUse" "post_tool_use.py"
Add-Hook "Stop" "session_end.py"

$settings | ConvertTo-Json -Depth 20 | Set-Content $SettingsPath -Encoding UTF8
Write-Host "Done. Run 'python -m cortex_memory.cli init' to create the database."
