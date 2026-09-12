; Ensemble-AI-Studio — Inno Setup script (packaging plan §2.2)
;
; Build (version MUST be passed in; there is exactly one source of truth for it,
; app/pyproject.toml, and build.ps1 reads it from there):
;
;   iscc /DMyAppVersion=0.1.0 packaging\installer.iss
;
; What this installer deliberately does NOT do (§2.2):
;   * no file associations / no shell integration  -> there is no [Registry] section
;   * no service, no scheduled task, no auto-update
;   * no writes outside {app} (the app itself writes only to %APPDATA%)
;   * uninstall keeps the user's data (%APPDATA%\Ensemble-AI-Studio); it only tells
;     the user where it is and how to remove it by hand.

#ifndef MyAppVersion
  ; Sentinel that is obviously not a real release, so a manual iscc run without
  ; /DMyAppVersion cannot silently ship a misnumbered Setup.exe.
  #define MyAppVersion "0.0.0-UNSET"
  #pragma message "WARNING: MyAppVersion was not passed; pass /DMyAppVersion=<x.y.z>."
#endif

#define MyAppName "Ensemble-AI-Studio"
#define MyAppPublisher "Ensemble-AI-Studio contributors"
#define MyAppURL "https://github.com/Whiteecch/Ensemble-AI-Studio"
#define MyAppExeName "Ensemble-AI-Studio.exe"
; The one-folder PyInstaller output that this installer wraps (packaging plan §2.3).
; Relative paths are resolved against this script's own directory.
#define BuildDir "..\dist"
#define ProductDir "Ensemble-AI-Studio-" + MyAppVersion + "-onedir"
; User data directory the app itself creates on first run (§1.1 / §三).
#define UserDataDir "{userappdata}\Ensemble-AI-Studio"

[Setup]
; A fixed AppId keeps upgrades in place (same product, new version) instead of
; installing side by side.
AppId={{8B2E1F4A-3C7D-4B9E-9A21-5F0D6C7E8A41}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
; {autopf} = Program Files (admin) or %LOCALAPPDATA%\Programs (non-admin).
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
; The payload is 64-bit only (Python 3.13 x64 + Qt6 x64). Without these two
; directives Inno runs the whole install in 32-bit mode, and then:
;   * {autopf} resolves to "C:\Program Files (x86)" - the README's advertised
;     default ("C:\Program Files\Ensemble-AI-Studio") would simply be wrong;
;   * 64-bit binaries land under the 32-bit folder, so anything that classifies
;     a program by its directory misreads it;
;   * the uninstall entry is written to the 32-bit registry view
;     (WOW6432Node), where the native view - PowerShell's default, most
;     inventory / silent-uninstall tooling - never sees it: installed but
;     unlisted, and hard to remove.
; x64compatible = plain x64 plus ARM64 under x64 emulation. A 32-bit Windows
; host cannot run this payload at all, so refusing to install there is the
; honest behaviour rather than installing something that cannot start.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Let the user pick "install for me only" from the dialog: that path needs no UAC.
; Without this, a non-admin user has no way to avoid the elevation prompt.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
; This script references {userappdata} (the user-data folder the app itself uses).
; Inno warns "UsedUserAreasWarning" about per-user paths in an all-users install.
; The warning is about *changing* per-user state; this installer only ever *opens*
; that folder and *tells* the user about it, so it is acknowledged here on purpose.
; Note the folder belongs to the user who runs the app - in the normal case (UAC
; elevation by the same user) that is also the user running this installer.
UsedUserAreasWarning=no
DisableProgramGroupPage=yes
AllowNoIcons=yes
; Show the MIT license and make the user accept it.
LicenseFile=..\LICENSE
OutputDir={#BuildDir}\release
OutputBaseFilename={#MyAppName}-{#MyAppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Nothing here needs a reboot, and the app never writes to {app} after install.
UninstallDisplayIcon={app}\{#MyAppExeName}
CreateUninstallRegKey=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Everything the one-folder build produced (exe + _internal/, which is where
; paths.resource_dir() looks in frozen onedir mode).
Source: "{#BuildDir}\{#ProductDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
; Finished-page entry that opens the user data folder. This is the "how do I get
; rid of it later" answer promised by §2.2, shown exactly when the user looks for it.
Filename: "{#UserDataDir}"; Description: "Open the user-data folder (characters, scenes, runs, settings)"; Flags: postinstall shellexec skipifsilent unchecked

[UninstallDelete]
; Nothing: {app} is removed by the generated uninstaller, and the user's own data
; lives in %APPDATA% (kept on purpose, §2.2 / §三).

[Code]
// On uninstall, be explicit that the user's creations survived and how to delete
// them. Doing this silently is how people lose (or endlessly re-find) their data.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{#UserDataDir}');
    if DirExists(DataDir) then
      MsgBox('Uninstall complete.' + #13#10 + #13#10 +
             'Your characters, scenes, information library, saved runs and settings were kept in:' + #13#10 +
             DataDir + #13#10 + #13#10 +
             'Delete that folder by hand if you want them gone too.',
             mbInformation, MB_OK);
  end;
end;
