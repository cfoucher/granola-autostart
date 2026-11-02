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
; The app has no windows for Restart Manager to close, so setup ends its processes itself (StopAppProcesses).
CloseApplications=no
RestartApplications=no
; A second setup (a double-click while an update installs, or two updates at once) exits instead of racing the first.
; The Global name covers a setup in another Windows session of the same user too (Remote Desktop, say).
SetupMutex=GranolaAutostartSetup,Global\GranolaAutostartSetup
; Setup runs unelevated and writes only the user's own folders, so the mitigation has little to guard. Inno Setup passes
; it on to the app that setup starts afterwards when setup never asked for administrator rights, as here, and the app
; would then be unable to follow the user's junctions (a OneDrive Personal Vault, say), after every update. Inno Setup
; 6.7 added the directive, so an older compiler skips it.
#if Ver >= 0x06070000
RedirectionGuard=no
#endif

[Files]
; Entries install in the order written: the libraries first, the exe last. The exe holds the app's own Python code,
; and that has to run on the libraries it was built with. So a setup that stops before the exe is in leaves the old
; exe, and RestoreLibraries puts the old libraries back beside it. One that stops after the exe is in has the old exe
; copied back too (RestoreExe), because Inno Setup keeps a file it replaced.
; dist\GranolaAutostart holds just the exe and _internal, and anything else PyInstaller adds goes with the libraries.
Source: "..\dist\GranolaAutostart\*"; Excludes: "\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\GranolaAutostart\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion; AfterInstall: ExeInstalled

[Icons]
; Opens Settings, which also starts the app if it isn't running.
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--settings"; AppUserModelID: "{#AppUserModelID}"

[Registry]
; Started at sign-in through the user's startup list, because Task Scheduler is denied on managed machines.
; A source checkout's --install writes the same entry (processes.py, RUN_KEY and RUN_VALUE); keep the two alike.
; General has a switch for it, so an upgrade keeps the user's choice (KeepSignInChoice).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "GranolaAutostart"; ValueData: """{app}\{#AppExe}"" --supervise"; Flags: uninsdeletevalue; Check: KeepSignInChoice
; Windows' Startup Apps keeps its own on/off mark for the entry (processes.py, STARTUP_APPROVED_KEY). Setup leaves it as
; it is, so an upgrade keeps the user's choice there too. Uninstalling removes it, so a later install isn't started off:
; no value is written, and the key isn't created if Windows hasn't (only the removal is recorded).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"; ValueType: none; ValueName: "GranolaAutostart"; Flags: uninsdeletevalue dontcreatekey
; The app writes its notification name and icon here (notify.py); uninstalling removes them.
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\{#AppUserModelID}"; Flags: uninsdeletekey

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--supervise"; Description: "Start {#AppName} now"; Flags: nowait postinstall; Check: StartAfterInstall

[UninstallDelete]
; The old libraries of an update that couldn't delete them (a program held a file) don't keep the folder alive. The
; name can carry a suffix: SetAsideLibraries falls back to one when an earlier setup's folder is still there. This
; also covers SetAsideNote and SetAsideExe, which sit beside them and are only left behind by a setup that ended badly.
Type: filesandordirs; Name: "{app}\_internal.old*"

[Code]
var
  InstallFinished: Boolean;
  Upgrading: Boolean;
  // This setup stopped the app, so it is this setup's to start again if it ends without finishing.
  StoppedApp: Boolean;
  LibrariesSetAside: Boolean;
  // Where SetAsideLibraries put them: _internal.old, or a name of its own when that folder couldn't be deleted.
  SetAsidePath: String;
  // The exe in place may not be the old one, whose copy is SetAsideExe: the new exe is in (ExeInstalled), or putting the
  // old one back failed (RestoreExe).
  ExeReplaced: Boolean;
  // An earlier setup's note can't be read (RecoverFromKilledSetup), so _internal may be half-copied.
  NoteUnreadable: Boolean;

// A file that holds the name of the set-aside libraries' folder while a setup is replacing them. A setup that is killed
// never reaches DeinitializeSetup, so the next one finds the note: the folder it names is the only good copy, and the
// _internal beside it is half-copied. The name starts with _internal.old, so [UninstallDelete] removes it too, and
// DeleteOldLibraries, which only deletes folders, leaves it alone.
function SetAsideNote: String;
begin
  Result := ExpandConstant('{app}\_internal.old.where');
end;

// A copy of the old exe, made with the set-aside libraries. Inno Setup rolls back a setup that fails after its files
// are in, but only deletes the files that weren't there before: the new libraries, which weren't, go, and the new exe,
// which replaced a file, stays. The old libraries then need the old exe back beside them, not the new one.
function SetAsideExe: String;
begin
  Result := ExpandConstant('{app}\_internal.old.exe');
end;

// Ends the processes of this install's exe, the supervisor first so it can't start the watcher again, and comes back
// once none is left (the result code is 0) or after 20 seconds (1). PowerShell does it, matching ExecutablePath as
// processes.stop_script does, because the exe can't be relied on to run: on half-copied libraries it shows an error
// box, which would hang a silent setup. Another copy of the exe, a source checkout say, is left alone. The script has
// no double quotes, so it goes through the command line as it is. It tries to end the processes once more before it
// gives up, and the process list gives up after 10 seconds. False when PowerShell couldn't be run, and a line in the
// log says if it ended any other way than 0. Both the uninstaller and the installer use it.
function StopAppProcesses(Exe: String): Boolean;
var
  Quoted: String;
  ResultCode: Integer;
begin
  Quoted := Exe;
  // PowerShell reads a curly single quote (U+2018 to U+201B) as a single quote, so each is doubled like the plain one.
  StringChangeEx(Quoted, '''', '''''', True);
  StringChangeEx(Quoted, #$2018, #$2018#$2018, True);
  StringChangeEx(Quoted, #$2019, #$2019#$2019, True);
  StringChangeEx(Quoted, #$201A, #$201A#$201A, True);
  StringChangeEx(Quoted, #$201B, #$201B#$201B, True);
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -Command "' +
    '$ErrorActionPreference = ''Stop''; ' +
    '$exe = ''' + Quoted + '''; ' +
    '$end = (Get-Date).AddSeconds(20); ' +
    'while ($true) { ' +
    '$procs = @(Get-CimInstance Win32_Process -OperationTimeoutSec 10 | Where-Object { $_.ExecutablePath -eq $exe }); ' +
    'if ($procs.Count -eq 0) { exit 0 }; ' +
    '$procs | Sort-Object { if ($_.CommandLine -like ''*--supervise*'') { 0 } else { 1 } } | ' +
    'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; ' +
    'if ((Get-Date) -gt $end) { exit 1 }; ' +
    'Start-Sleep -Milliseconds 250 }"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if not Result then
    Log('Could not run PowerShell, so the app''s processes were not stopped.')
  else if ResultCode <> 0 then
    Log('PowerShell ended with ' + IntToStr(ResultCode) + ', so some of the app''s processes may still be running.');
end;

procedure StopRunningApp;
var
  Exe: String;
  WasRunning: Boolean;
begin
  Exe := ExpandConstant('{app}\{#AppExe}');
  if FileExists(Exe) then
  begin
    // The supervisor or the watcher (the mutex names are processes.py's). Only an app that was running is this setup's
    // to start again: someone who chose Quit, or never started it, has neither.
    WasRunning := CheckForMutexes('Local\GranolaAutostart,Local\GranolaAutostartSupervisor');
    // It runs either way, as Settings may be open. The processes are gone when it returns, so their files are free.
    // Added to, not set: PrepareToInstall runs again after Back, when the app is already stopped.
    if StopAppProcesses(Exe) and WasRunning then
      StoppedApp := True;
  end;
end;

// Whether the app starts at sign-in is a switch in General, kept as the Run value. A fresh install turns it on. An
// upgrade writes it back only if it is still there, so a user who switched it off isn't put back on.
function KeepSignInChoice: Boolean;
begin
  Result := (not Upgrading) or RegValueExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'GranolaAutostart');
end;

// Whether [Run] starts the app. An interactive setup offers it, and a first install starts it. A silent update (the
// app's own Install update, say) starts it only if this setup stopped it, so an app that was quit stays quit.
function StartAfterInstall: Boolean;
begin
  Result := (not WizardSilent) or StoppedApp or (not Upgrading);
end;

// Deletes the old libraries: _internal.old, and any that an earlier setup set aside under a name of its own. A folder
// a program still holds a file of stays, and the next setup or the uninstaller deletes it.
procedure DeleteOldLibraries;
var
  Found: TFindRec;
begin
  if FindFirst(ExpandConstant('{app}\_internal.old*'), Found) then
  try
    repeat
      if (Found.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
        DelTree(ExpandConstant('{app}\') + Found.Name, True, True, True);
    until not FindNext(Found);
  finally
    FindClose(Found);
  end;
end;

// The new libraries and exe are both in, so the old ones go, and the note about where they are first: a setup killed
// in the middle of deleting them has a complete _internal, and mustn't take it for a half-copied one. When the note
// can't be deleted they stay, for the same reason: they would be half-deleted under a note that still names them.
procedure DiscardOldLibraries;
begin
  DeleteFile(SetAsideNote);
  if FileExists(SetAsideNote) then
  begin
    Log('Could not delete ' + SetAsideNote + ', so the old libraries stay.');
    Exit;
  end;
  DeleteOldLibraries;
  DeleteFile(SetAsideExe);
end;

// Sets Folder to where an earlier setup that was killed put the old libraries, or '' when there is none to recover. Only
// a folder name of this kind counts, not whatever the note says: it is a file in a folder the user can write to. False
// when there is a note that can't be read, which may be about folders that are the only good copy: nothing can be
// said about them, so setup mustn't go on to delete them.
function SetAsideByKilledSetup(var Folder: String): Boolean;
var
  Written: AnsiString;
  Name: String;
begin
  Folder := '';
  Result := True;
  if not FileExists(SetAsideNote) then
    Exit;
  if not LoadStringFromFile(SetAsideNote, Written) then
  begin
    Result := False;
    Exit;
  end;
  Name := Trim(Written);
  if (Pos('_internal.old', Name) = 1) and (Pos('\', Name) = 0) and (Pos('/', Name) = 0) and (Pos(':', Name) = 0) and
      DirExists(ExpandConstant('{app}\') + Name) then
    Folder := ExpandConstant('{app}\') + Name;
end;

// An upgrade replaces the bundled libraries wholesale, so nothing from an older build lingers. The old ones are set
// aside, not deleted, until the new ones are in: a setup that fails then puts them back (RestoreLibraries), so the
// app is never left with an exe and no libraries.
function SetAsideLibraries: Boolean;
var
  Attempt: Integer;
  Exe, Target: String;
  Noted: Boolean;
begin
  Result := True;
  if not DirExists(ExpandConstant('{app}\_internal')) then
    Exit;
  // Left by an earlier setup that couldn't delete it. If one is still there (a program holds a file in it), it
  // would block this rename, and so every later update, so these go under a name of their own.
  DeleteOldLibraries;
  Target := ExpandConstant('{app}\_internal.old');
  if DirExists(Target) then
    Target := Target + '.' + GetDateTimeString('yyyymmddhhnnss', #0, #0);
  // The note is written before the move, so a setup killed any time after it finds where the old libraries are, and
  // the exe is copied now, before anything of the new version is in. If the note or the copy can't be made, this fails
  // before the move: a setup killed after it would have no way to find the old libraries. A folder with libraries but
  // no exe is a first install that was killed. It has no exe to copy, and a copy left by an earlier setup is deleted,
  // so that RestoreExe can't put back an exe that doesn't belong.
  Exe := ExpandConstant('{app}\{#AppExe}');
  Noted := SaveStringToFile(SetAsideNote, ExtractFileName(Target), False);
  if Noted then
  begin
    if FileExists(Exe) then
      Noted := CopyFile(Exe, SetAsideExe, False)
    else
    begin
      DeleteFile(SetAsideExe);
      Noted := not FileExists(SetAsideExe);
    end;
  end;
  if not Noted then
  begin
    Log('Could not write ' + SetAsideNote + ' or ' + SetAsideExe + ', so the old libraries are not set aside.');
    DeleteFile(SetAsideNote);
    DeleteFile(SetAsideExe);
    Result := False;
    Exit;
  end;
  for Attempt := 1 to 5 do
  begin
    if RenameFile(ExpandConstant('{app}\_internal'), Target) then
    begin
      SetAsidePath := Target;
      LibrariesSetAside := True;
      Exit;
    end;
    // A program (antivirus, say) may still hold one of the files for a moment.
    Sleep(200);
  end;
  DeleteFile(SetAsideNote);
  DeleteFile(SetAsideExe);
  Result := False;
end;

// Puts the old exe back, from the copy SetAsideLibraries made, and deletes the copy. It does so whenever there is a
// copy, not only after the new exe went in (ExeReplaced), because the exe in place can't be taken for the old one:
// Inno Setup deletes the old exe before it renames the new one into place, so a setup that fails then has no exe; one
// that was killed after the new exe was in, but before ExeInstalled, has the new one; and a note that outlived a
// finished update (RecoverFromKilledSetup takes it for a killed setup) has the new one too. Copying the old exe over
// itself is harmless. The exe in place may be held for a moment (antivirus, say), so it tries for a while. When it
// can't, ExeReplaced is True and the copy stays, and the app isn't started.
procedure RestoreExe;
var
  Attempt: Integer;
begin
  if not FileExists(SetAsideExe) then
    Exit;
  for Attempt := 1 to 30 do
  begin
    if CopyFile(SetAsideExe, ExpandConstant('{app}\{#AppExe}'), False) then
    begin
      ExeReplaced := False;
      DeleteFile(SetAsideExe);
      Exit;
    end;
    Sleep(500);
  end;
  ExeReplaced := True;
  Log('Could not put the old exe back from ' + SetAsideExe + '.');
end;

// Setup ended without finishing: whatever it copied of the new libraries goes, and the old ones come back, from
// wherever SetAsideLibraries put them, with the old exe. A program may hold one of the new files for a moment
// (antivirus, say), which stops them being deleted and so the old folder being renamed back, so it tries for longer
// than setting aside does: there is nothing to fall back on. When it still can't, LibrariesSetAside stays True, and the
// note is written again, because ExeInstalled deleted it once the new exe was in: the next setup finds the old
// libraries (RecoverFromKilledSetup) as it would after a killed one.
procedure RestoreLibraries;
var
  Attempt: Integer;
begin
  if not LibrariesSetAside then
    Exit;
  for Attempt := 1 to 30 do
  begin
    DelTree(ExpandConstant('{app}\_internal'), True, True, True);
    if RenameFile(SetAsidePath, ExpandConstant('{app}\_internal')) then
    begin
      LibrariesSetAside := False;
      DeleteFile(SetAsideNote);
      RestoreExe;
      Exit;
    end;
    Sleep(500);
  end;
  Log('Could not put the old libraries back from ' + SetAsidePath + '.');
  if not SaveStringToFile(SetAsideNote, ExtractFileName(SetAsidePath), False) then
    Log('Could not write ' + SetAsideNote + ', so the next setup will not find them.');
end;

// An earlier setup that was killed after it set the old libraries aside left them where SetAsideNote says, with a
// half-copied _internal beside them. They are the only good copy, so this puts them back, which leaves the folder as
// it was before that setup: the exe in it runs again, and what follows is an ordinary upgrade. SetupMutex means that
// setup is over: a live one would have turned this one away. A setup killed after the new exe was in has no note (see
// ExeInstalled), so what it left is a complete new version, and an ordinary upgrade replaces that. False when setup
// can't go on: the old libraries can't be put back, or the note can't be read (nothing is deleted then).
function RecoverFromKilledSetup: Boolean;
var
  Folder: String;
begin
  Result := True;
  if not SetAsideByKilledSetup(Folder) then
  begin
    Log('Could not read ' + SetAsideNote + ', which an earlier setup left, so nothing is deleted and setup stops.');
    NoteUnreadable := True;
    Result := False;
    Exit;
  end;
  if Folder = '' then
    Exit;
  Log('An earlier setup was stopped part-way. Putting its old libraries back from ' + Folder + '.');
  SetAsidePath := Folder;
  LibrariesSetAside := True;
  RestoreLibraries;
  // Setup ends here when they can't be put back, and the note stays for the next one. LibrariesSetAside stays too,
  // so DeinitializeSetup tries once more, and doesn't start an app that has no libraries.
  Result := not LibrariesSetAside;
end;

// Runs once the new exe is in, which is the last file: the new libraries went in before it, so the pair is whole. A
// setup that is killed from here on leaves a working new version, which the next setup mustn't mistake for a
// half-copied one, so the note goes now. One that fails or is cancelled still rolls back the new libraries, and takes
// the old exe and libraries back (RestoreLibraries): they are kept until setup is past the point where it can fail.
procedure ExeInstalled;
begin
  ExeReplaced := FileExists(SetAsideExe);
  DeleteFile(SetAsideNote);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  // Before any file is copied: the exe is already there when this is an upgrade.
  Upgrading := FileExists(ExpandConstant('{app}\{#AppExe}'));
  Result := 'Setup could not move the old version''s files out of the way. Close any program using the Granola Autostart folder, then run setup again.';
  // First, because an app left on half-copied libraries by a killed setup holds them open, and they can't be put back
  // (RecoverFromKilledSetup) or moved (SetAsideLibraries) while it runs.
  StopRunningApp;
  if not RecoverFromKilledSetup then
    Exit;
  if SetAsideLibraries then
    Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Setup is past the point where it can be rolled back, so the old version has nothing left to come back for.
    InstallFinished := True;
    DiscardOldLibraries;
  end;
end;

// Setup that ends without finishing (cancelled, or stopped by an error) has usually stopped the app already, and
// nothing would start it again until the next sign-in. So it puts the old libraries and exe back first, and then starts
// the app, but only if this setup stopped it and the old version is back: an exe on libraries that aren't its own only
// shows an error. A second setup that SetupMutex turned away stopped nothing, so it starts nothing either, and can't
// start the app in the middle of the first one's update.
procedure DeinitializeSetup;
var
  Exe: String;
  ResultCode: Integer;
begin
  if InstallFinished then
    Exit;
  try
    Exe := ExpandConstant('{app}\{#AppExe}');
  except
    // Setup ended before it had an install folder, so it has stopped nothing.
    Exit;
  end;
  RestoreLibraries;
  if LibrariesSetAside or ExeReplaced or NoteUnreadable then
    Log('The old version is not back, so the app is not started.')
  else if StoppedApp and FileExists(Exe) then
    Exec(Exe, '--supervise', ExpandConstant('{app}'), SW_HIDE, ewNoWait, ResultCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // The app's processes hold its files, so they end before any is deleted. This is setup's own PowerShell, as in
  // StopRunningApp, and not the exe's --stop: the exe can't be relied on to run, on libraries a failed setup left
  // half-copied say, and the uninstaller would wait for it.
  if CurUninstallStep = usUninstall then
    StopAppProcesses(ExpandConstant('{app}\{#AppExe}'));
  if CurUninstallStep = usPostUninstall then
  begin
    if SuppressibleMsgBox('Keep your Granola Autostart settings and calendar addresses?' + #13#10#13#10 +
        'Choose No to delete them from this PC.', mbConfirmation, MB_YESNO, IDYES) = IDNO then
      DelTree(ExpandConstant('{localappdata}\granola-autostart'), True, True, True);
  end;
end;
