# RDC Agent Status - dark-themed control dashboard (WPF).
# Open on demand from the desktop shortcut; nothing here runs in background.
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase

$dcDir  = Join-Path $env:LOCALAPPDATA 'RDC-Agent'
$report = Join-Path $dcDir 'guardian-status.json'
$outLog = Join-Path $dcDir 'remote-output.log'
$gLog   = Join-Path $dcDir 'guardian.log'

# ---- dark palette (Catppuccin-inspired) ----
$bg     = '#1E1E2E'   # base
$bgCard = '#282838'   # cards
$fg     = '#CDD6F4'   # text
$dim    = '#6C7086'   # muted
$green  = '#A6E3A1'
$yellow = '#F9E2AF'
$red    = '#F38BA8'
$accent = '#89B4FA'

function Get-AgentFacts {
    $facts = @{ Pid = '-'; Conns = 0; Uptime = '-'; StatusText = 'UNKNOWN'; StatusColor = $yellow }
    $st = $null
    if (Test-Path $report) { try { $st = Get-Content $report -Raw | ConvertFrom-Json } catch { } }
    if ($st) {
        $facts.StatusText = $st.Status
        switch -Wildcard ($st.Status) {
            'OK*'      { $facts.StatusColor = $green }
            'BOOTING'  { $facts.StatusColor = $yellow }
            'NETWORK*' { $facts.StatusColor = $yellow }
            'NEEDS*'   { $facts.StatusColor = $red }
            default    { $facts.StatusColor = $red }
        }
        $facts.LastCheck = $st.Timestamp
        $facts.LastRestart = if ($st.LastRestart -like '0001*') { 'never (this boot)' } else { $st.LastRestart }
        $facts.Failures = $st.ConsecutiveFailures
    }
    foreach ($p in (Get-Process -Name node)) {
        $c = (Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $p.Id)).CommandLine
        if ($c -match 'desktop-commander' -and $c -match '\sremote\b') {
            $facts.Pid = $p.Id
            $facts.Uptime = '{0:mm\m\ ss\s}' -f ((Get-Date) - $p.StartTime)
            $conns = Get-NetTCPConnection -OwningProcess $p.Id -State Established -ErrorAction SilentlyContinue
            $facts.Conns = @($conns).Count
        }
    }
    return $facts
}

function Get-LogTail([string]$path, [int]$n) {
    if (Test-Path -LiteralPath $path) {
        return (Get-Content -LiteralPath $path -Tail $n) -join "`n"
    }
    return '(log not created yet)'
}

# ---- window ----
$w = New-Object System.Windows.Window
$w.Title = 'RDC Agent Control'
$w.Width = 760; $w.Height = 560
$w.WindowStartupLocation = 'CenterScreen'
$w.Background = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($bg)))
$w.FontFamily = 'Segoe UI'

$root = New-Object System.Windows.Controls.StackPanel
$root.Margin = '20,16'
[void]$root.Children.Add((New-Object System.Windows.Controls.TextBlock -Property @{
    Text = 'REMOTE DESKTOP COMMANDER'
    FontSize = 22; FontWeight = 'Bold'; Foreground = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($accent)))
}))
[void]$root.Children.Add((New-Object System.Windows.Controls.TextBlock -Property @{
    Text = 'agent health  -  relay session  -  guardian decisions'
    FontSize = 12; Margin = '0,2,0,14'; Foreground = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($dim)))
}))

$statusCard = New-Object System.Windows.Controls.Border -Property @{
    Background = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($bgCard)))
    CornerRadius = '10'; Padding = '16,12'; Margin = '0,0,0,10'
}
$statusPanel = New-Object System.Windows.Controls.StackPanel
$statusCard.Child = $statusPanel
[void]$root.Children.Add($statusCard)

$detailsCard = New-Object System.Windows.Controls.Border -Property @{
    Background = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($bgCard)))
    CornerRadius = '10'; Padding = '16,12'; Margin = '0,0,0,10'
}
$detailsPanel = New-Object System.Windows.Controls.StackPanel
$detailsCard.Child = $detailsPanel
[void]$root.Children.Add($detailsCard)

$logCard = New-Object System.Windows.Controls.Border -Property @{
    Background = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($bgCard)))
    CornerRadius = '10'; Padding = '12,10'; Margin = '0,0,0,10'
}
$logBox = New-Object System.Windows.Controls.TextBox -Property @{
    IsReadOnly = $true; FontFamily = 'Consolas'; FontSize = 11.5
    Background = 'Transparent'; BorderThickness = 0
    Foreground = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($fg)))
    Height = 150; TextWrapping = 'NoWrap'; VerticalScrollBarVisibility = 'Auto'
}
$logCard.Child = $logBox
[void]$root.Children.Add($logCard)

# ---- app icon ----
$iconPath = Join-Path $PSScriptRoot 'rdc-icon.ico'
if (Test-Path -LiteralPath $iconPath) {
    try {
        Add-Type -AssemblyName System.Drawing
        $w.Icon = New-Object System.Drawing.Icon ($iconPath)
    } catch { }
}

# ---- refresh logic ----
function Add-Row([System.Windows.Controls.StackPanel]$panel, [string]$label, [string]$value, [string]$valueColor) {
    $sp = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; Margin = '0,3' }
    [void]$sp.Children.Add((New-Object System.Windows.Controls.TextBlock -Property @{
        Text = ($label + '  '); Width = 150
        Foreground = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($dim)))
    }))
    [void]$sp.Children.Add((New-Object System.Windows.Controls.TextBlock -Property @{
        Text = $value; FontWeight = 'SemiBold'
        Foreground = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($valueColor)))
    }))
    [void]$panel.Children.Add($sp)
}

function Invoke-Refresh {
    $f = Get-AgentFacts
    foreach ($child in @($statusPanel.Children)) { $statusPanel.RemoveChild($child) }
    foreach ($child in @($detailsPanel.Children)) { $detailsPanel.RemoveChild($child) }

    $statusRow = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; Margin = '0,0,0,8' }
    [void]$statusRow.Children.Add((New-Object System.Windows.Controls.TextBlock -Property @{
        Text = '  ' + $f.StatusText + '  '
        FontSize = 16; FontWeight = 'Bold'
        Foreground = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($bg)))
        Background = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($f.StatusColor)))
        Padding = '8,4'
    }))
    [void]$statusRow.Children.Add((New-Object System.Windows.Controls.TextBlock -Property @{
        Text = ('   agent PID ' + $f.Pid + '   |   relay connections: ' + $f.Conns)
        FontSize = 14; VerticalAlignment = 'Center'
        Foreground = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($fg)))
    }))
    [void]$statusPanel.Children.Add($statusRow)

    Add-Row $detailsPanel 'guardian check'   $f.LastCheck    $fg
    Add-Row $detailsPanel 'consec. failures' $f.Failures     $(if ($f.Failures -gt 0) { $red } else { $green })
    Add-Row $detailsPanel 'last restart'     $f.LastRestart  $fg
    Add-Row $detailsPanel 'agent uptime'     $f.Uptime       $fg
    Add-Row $detailsPanel 'output log'       $outLog         $dim

    $logBox.Text = (Get-LogTail $gLog 14)
}

# ---- buttons ----
$buttons = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal' }
function New-Button([string]$text, [scriptblock]$onClick) {
    $b = New-Object System.Windows.Controls.Button -Property @{
        Content = $text; Padding = '14,7'; Margin = '0,0,10,0'; Cursor = 'Hand'
        Background = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($bgCard)))
        Foreground = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($fg)))
        BorderBrush = (New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($accent)))
    }
    $b.Add_Click($onClick)
    [void]$buttons.Children.Add($b)
}
New-Button 'Refresh' { Invoke-Refresh }
New-Button 'Restart agent' {
    Get-Process -Name node | ForEach-Object {
        $c = (Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $_.Id)).CommandLine
        if ($c -match 'desktop-commander') { Stop-Process -Id $_.Id -Force }
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'RdcAgentStart.ps1') | Out-Null
    Start-Sleep -Seconds 5; Invoke-Refresh
}
New-Button 'Run guardian now' {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'RdcGuardian.ps1') | Out-Null
    Invoke-Refresh
}
[void]$root.Children.Add($buttons)

$w.Content = $root
Invoke-Refresh
[void]$w.ShowDialog()
