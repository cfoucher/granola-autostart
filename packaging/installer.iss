; Per-user installer for Granola Autostart. Build it with build.ps1, which passes /DAppVersion.
; Installs without admin rights to %LOCALAPPDATA%\Programs\Granola Autostart.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Granola Autostart"
#define AppExe "GranolaAutostart.exe"
; Windows notifications are tied to this ID through the Start menu shortcut.
#define AppUserModelID "GranolaAutostart"

[Setup]
AppId={{F3D04E08-1CAA-481E-B0C2-76D8D0E00007}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Conrad Foucher
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=GranolaAutostart-{#AppVersion}-setup
SetupIconFile=..\build\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
; The app has no windows for Restart Manager to close, so it is stopped with --stop instead.
CloseApplications=no
RestartApplications=no

[Files]
Source: "..\dist\GranolaAutostart\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; An upgrade replaces the bundled libraries wholesale, so nothing from an older build lingers.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
; Opens Settings, which also starts the app if it isn't running.
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--settings"; AppUserModelID: "{#AppUserModelID}"

[Registry]
; Started at sign-in through the user's startup list, because Task Scheduler is denied on managed machines.
; A source checkout's --install writes the same entry (processes.py, RUN_KEY and RUN_VALUE); keep the two alike.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "GranolaAutostart"; ValueData: """{app}\{#AppExe}"" --supervise"; Flags: uninsdeletevalue
; The app writes its notification name and icon here (notify.py); uninstalling removes them.
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\{#AppUserModelID}"; Flags: uninsdeletekey

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--supervise"; Description: "Start {#AppName} now"; Flags: nowait postinstall

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopApp"

[Code]
procedure StopRunningApp;
var
  Exe: String;
  ResultCode: Integer;
begin
  Exe := ExpandConstant('{app}\{#AppExe}');
  if FileExists(Exe) then
  begin
    Exec(Exe, '--stop', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    // Give Windows a moment to release the files of the stopped processes.
    Sleep(1000);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningApp;
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if SuppressibleMsgBox('Keep your Granola Autostart settings and calendar addresses?' + #13#10#13#10 +
        'Choose No to delete them from this PC.', mbConfirmation, MB_YESNO, IDYES) = IDNO then
      DelTree(ExpandConstant('{localappdata}\granola-autostart'), True, True, True);
  end;
end;
