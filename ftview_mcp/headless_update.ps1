# Headless UPDATE import of one display - no View Studio UI.
#
# How it works (found 8 Oct 2026, see TESTING.md "Headless"):
#   View's Gfx-to-XML engine (RocketConversionManager.RktMgr) opens a display and hands it to
#   ViewPoint's exporter (ViewPointGfxXmlExport.GfxExport, CLSID 30703590...). In THIS process only,
#   we register our own class factory for that CLSID (CoRegisterClassObject - no registry change), so
#   the engine hands the opened display and graphics server to us. We then run View's own importer
#   (GfxXmlImport.GfxImport, IGfxImportWithVBA) in update mode and save through the display's
#   IComponent.Save, which writes the project's .gfx including the language string table.
#
# Limits: update mode only changes objects that already exist (this engine cannot create objects);
# must not run while the application is open in View Studio. The Python caller backs up, verifies
# and restores. Output: one JSON object on stdout.
#
#   powershell(x86) -File headless_update.ps1 -App FTVMCP_SANDBOX -Display NAME -Xml C:\x\NAME.xml
param(
    [Parameter(Mandatory = $true)][string]$App,
    [Parameter(Mandatory = $true)][string]$Display,
    [Parameter(Mandatory = $true)][string]$Xml,
    [string]$Server = '',
    [ValidateSet('Local', 'Global')][string]$Directory = 'Local'
)
$ErrorActionPreference = 'Stop'
if ([Environment]::Is64BitProcess) { throw 'run this script in 32-bit PowerShell (SysWOW64)' }
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Reflection;
using System.Runtime.InteropServices;

[ComVisible(true), Guid("736E706C-5B5C-45E8-A5F2-6667CDE95489"), InterfaceType(ComInterfaceType.InterfaceIsDual)]
public interface IGfxExport {
    void Initialize([MarshalAs(UnmanagedType.IUnknown)] object pGfxServer);
    void ExportXML([MarshalAs(UnmanagedType.IUnknown)] object pGfxDisplay, [MarshalAs(UnmanagedType.BStr)] string bsFileName);
    void SetProgress([MarshalAs(UnmanagedType.IUnknown)] object pGfxProgress);
}

[ComImport, Guid("64C995E9-3057-4B2A-80A0-E5AD16E380A7"), InterfaceType(ComInterfaceType.InterfaceIsDual)]
public interface IGfxImportWithVBA {
    void Initialize([MarshalAs(UnmanagedType.IUnknown)] object pGfxServer);
    void ImportXML([MarshalAs(UnmanagedType.IUnknown)] object pDisplay, [MarshalAs(UnmanagedType.BStr)] string file, int bUpdate);
    void SetProgress([MarshalAs(UnmanagedType.IUnknown)] object pGfxProgress);
    void ImportXMLVba([MarshalAs(UnmanagedType.IUnknown)] object pDisplay, [MarshalAs(UnmanagedType.BStr)] string file, int bUpdate, out int vbaStatus);
}

[ComImport, Guid("060FEBAD-EC0F-47FE-A4F8-74EA48106AA2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IComponentX {
    void Open([MarshalAs(UnmanagedType.LPWStr)] string name);
    void Save(int bSaveAs);
    void GetProject([MarshalAs(UnmanagedType.IUnknown)] out object prj);
    void GetSAFramework([MarshalAs(UnmanagedType.IUnknown)] out object fw);
    void ResizeToFit();
    void SaveModified();
    void GetName([MarshalAs(UnmanagedType.BStr)] out string name);
    void OnAddComponent([MarshalAs(UnmanagedType.LPWStr)] string logical, int bNew);
    void GetFrameWindow(out int hwnd);
    void GetComponentCategory([MarshalAs(UnmanagedType.IUnknown)] out object cat);
    void IsOnTop(out int onTop);
    void NotifyProjectSetFocus();
    void OnProjectClose();
    void NotifyCacheFlush(out int flushed);
    void IsPaused(out int paused);
    void get_KeepTo(out int v);
    void put_KeepTo(int v);
    void OnClose(int bAsync);
    void SetModifiedFlag(int bModified);
}

[ComImport, Guid("00000001-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IClassFactory {
    [PreserveSig] int CreateInstance(IntPtr outer, ref Guid riid, out IntPtr obj);
    [PreserveSig] int LockServer(bool l);
}

[ComVisible(true), ClassInterface(ClassInterfaceType.None)]
public class UpdateSink : IGfxExport {
    public static string XmlFile = "", Expected = "";
    public static StringBuilder Log = new StringBuilder();
    public static bool Imported = false, Saved = false;
    object server;
    public void Initialize(object s) { server = s; }
    public void SetProgress(object p) { }
    public void ExportXML(object display, string file) {
        var comp = (IComponentX)display;
        string name; comp.GetName(out name);
        if (!string.Equals(name, Expected, StringComparison.OrdinalIgnoreCase)) {
            Log.Append("engine opened '" + name + "', expected '" + Expected + "'; nothing changed. ");
            return;
        }
        var imp = (IGfxImportWithVBA)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("8C6834DE-C03C-4A9D-8F36-134561414C8A")));
        imp.Initialize(server);
        int vba; imp.ImportXMLVba(display, XmlFile, 1, out vba);
        Imported = true;
        Log.Append("import ok (vbaStatus " + vba + "). ");
        comp.SetModifiedFlag(1);
        comp.Save(0);
        Saved = true;
        Log.Append("saved. ");
    }
}

[ComVisible(true), ClassInterface(ClassInterfaceType.None)]
public class SinkFactory : IClassFactory {
    public int CreateInstance(IntPtr outer, ref Guid riid, out IntPtr obj) {
        IntPtr unk = Marshal.GetIUnknownForObject(new UpdateSink());
        int hr = Marshal.QueryInterface(unk, ref riid, out obj);
        Marshal.Release(unk);
        return hr;
    }
    public int LockServer(bool l) { return 0; }
}

public static class Hook {
    [DllImport("ole32.dll")] static extern int CoRegisterClassObject(ref Guid clsid, [MarshalAs(UnmanagedType.IUnknown)] object unk, uint ctx, uint flags, out uint cookie);
    [DllImport("ole32.dll")] static extern int CoRevokeClassObject(uint cookie);
    static uint cookie;
    public static int Register() { Guid g = new Guid("30703590-AAFB-48A2-8998-9AFF8223F36E"); return CoRegisterClassObject(ref g, new SinkFactory(), 1, 1, out cookie); }
    public static void Revoke() { CoRevokeClassObject(cookie); }
}

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
$result = @{ app = $App; display = $Display; xml = $Xml; ok = $false }
if (-not (Test-Path -LiteralPath $Xml)) { $result.error = "xml not found: $Xml"; $result | ConvertTo-Json -Compress; exit }
[UpdateSink]::XmlFile = (Resolve-Path -LiteralPath $Xml).Path
[UpdateSink]::Expected = $Display
$hr = [Hook]::Register()
if ($hr -ne 0) { $result.error = "CoRegisterClassObject failed hr=$hr"; $result | ConvertTo-Json -Compress; exit }
$area = 'RNA://$' + $Directory + '/' + $App
$scratch = Join-Path $env:TEMP ('ftv_hl_' + [guid]::NewGuid().ToString('N'))
$r = New-Object -ComObject 'RocketConversionManager.RktMgr.1'
try {
    $r.SetupApplication(0, $area)
    # plain [string]s: PowerShell-wrapped strings cause DISP_E_TYPEMISMATCH in COM calls
    $callArgs = [object[]]@([string]$area, [string]$Server, [string]$Display, [string]$scratch, [string]'', [string]'')
    $res = [RocketCall]::Call($r, 'ConvertDisplays', $callArgs, @($false, $false, $false, $false, $true, $true))
    $result.engine_errors = $res[5]
    $result.imported = [UpdateSink]::Imported
    $result.saved = [UpdateSink]::Saved
    $result.ok = [UpdateSink]::Imported -and [UpdateSink]::Saved
    if (-not $result.ok -and -not $result.error) { $result.error = 'display was not opened/updated by the engine' }
} catch {
    $result.error = $_.Exception.Message
} finally {
    $result.log = [UpdateSink]::Log.ToString()
    try { $r.CleanUp() } catch {}
    [Hook]::Revoke()
    if (Test-Path $scratch) { Remove-Item -Recurse -Force $scratch -ErrorAction SilentlyContinue }
}
$result | ConvertTo-Json -Compress
