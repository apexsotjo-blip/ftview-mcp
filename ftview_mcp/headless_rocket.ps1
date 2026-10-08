# Headless display listing/export through View's own "Rocket" Gfx-to-XML engine
# (RocketConversionManager.RktMgr, Gfx2XmlWorkflowEngine.dll). Must run in 32-bit PowerShell
# because the engine is a 32-bit in-process COM server. Read-only: it never modifies the application.
#
#   powershell(x86) -File headless_rocket.ps1 -Action list   -App MyPlant [-Server MyPlant]
#   powershell(x86) -File headless_rocket.ps1 -Action export -App MyPlant -Displays "A|B" -Out C:\dir
#
# Output: one JSON object on stdout.
param(
    [Parameter(Mandatory = $true)][ValidateSet('list', 'export')][string]$Action,
    [Parameter(Mandatory = $true)][string]$App,
    [string]$Server = '',
    [string]$Displays = '',
    [string]$Out = '',
    [ValidateSet('Local', 'Global')][string]$Directory = 'Local'
)
$ErrorActionPreference = 'Stop'
if ([Environment]::Is64BitProcess) { throw 'run this script in 32-bit PowerShell (SysWOW64)' }
Add-Type -TypeDefinition @'
using System;
using System.Reflection;
public static class RocketCall {
    public static object[] Call(object o, string method, object[] args, bool[] byref) {
        var pm = new ParameterModifier(args.Length);
        for (int i = 0; i < args.Length; i++) pm[i] = byref[i];
        o.GetType().InvokeMember(method, BindingFlags.InvokeMethod, null, o, args, new[] { pm }, null, null);
        return args;
    }
}
'@
if (-not $Server) { $Server = $App }
$area = 'RNA://$' + $Directory + '/' + $App
$result = @{ app = $App; server = $Server; area = $area; action = $Action }
$r = New-Object -ComObject 'RocketConversionManager.RktMgr.1'
try {
    $r.SetupApplication(0, $area)
    if ($Action -eq 'list') {
        $res = [RocketCall]::Call($r, 'ListDisplaysWithFolders', @($area, $Server, '', ''), @($false, $false, $true, $true))
        $names = @($res[2] -split '\|' | Where-Object { $_ })
        $folders = @($res[3] -split '\|' | Where-Object { $_ })
        $result.displays = $names
        $result.folders = $folders
    } else {
        if (-not $Out) { throw '-Out is required for export' }
        New-Item -ItemType Directory -Force $Out | Out-Null
        if (-not $Displays) {
            $res = [RocketCall]::Call($r, 'ListDisplaysWithFolders', @($area, $Server, '', ''), @($false, $false, $true, $true))
            $Displays = ($res[2].TrimEnd('|'))
        }
        $res = [RocketCall]::Call($r, 'ConvertDisplays', @($area, $Server, $Displays, $Out, '', ''),
                                  @($false, $false, $false, $false, $true, $true))
        $result.requested = @($Displays -split '\|' | Where-Object { $_ })
        $result.warnings = $res[4]
        $result.errors = $res[5]
        $result.out = $Out
    }
    $result.ok = $true
} catch {
    $result.ok = $false
    $result.error = $_.Exception.Message
} finally {
    try { $r.CleanUp() } catch {}
}
$result | ConvertTo-Json -Depth 4 -Compress
