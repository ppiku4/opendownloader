; OpenDownloader - Windows installer (Inno Setup 6)
; Built automatically by packaging\build_windows.bat  ->  dist\OpenDownloader-Setup.exe
; Manual build:  ISCC.exe /DAppVersion=1.0.0 packaging\installer.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "OpenDownloader"
#define AppExe "OpenDownloader.exe"

[Setup]
AppId={{8C1E4F2B-3D6A-4E8B-9F1C-0D5A7B2E6C41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=OpenDownloader
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
; Per-user install by default (no admin prompt): %LOCALAPPDATA%\Programs\OpenDownloader.
; Users can still choose "Install for all users" (Program Files) in the first dialog.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Qt 6 needs Windows 10 1903 or newer
MinVersion=10.0.18362
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=OpenDownloader-Setup
SetupIconFile=opendownloader.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
LZMAUseSeparateProcess=yes
SolidCompression=yes
WizardStyle=modern
; Close a running OpenDownloader before replacing its files (see also PrepareToInstall below)
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start OpenDownloader when I sign in to Windows (runs in the tray)"; GroupDescription: "Startup:"
Name: "torrents"; Description: "Open .torrent files and magnet links with OpenDownloader"; GroupDescription: "Torrents:"

[Files]
Source: "..\dist\OpenDownloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[InstallDelete]
; files from older builds that no longer exist in this one
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\OpenDownloaderHost.exe"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Browser extension (Chrome, Edge, Firefox)"; Filename: "{app}\extension"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"" --minimized"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
; Register the browser integration (native messaging host) for Chrome/Edge/Brave/Vivaldi/Firefox
Filename: "{app}\{#AppExe}"; Parameters: "--register-host"; StatusMsg: "Registering browser integration..."; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Parameters: "--associate-torrents"; StatusMsg: "Registering torrent files and magnet links..."; Tasks: torrents; Flags: runhidden waituntilterminated
Filename: "{app}\README.txt"; Description: "Show how to add the browser extension"; Flags: postinstall shellexec skipifsilent
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: postinstall nowait skipifsilent
; Help -> Check for updates runs Setup with /SILENT /RELAUNCH=1: start the updated app again
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: RelaunchAfterUpdate

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--quit"; Flags: runhidden waituntilterminated; RunOnceId: "QuitApp"
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM {#AppExe}"; Flags: runhidden waituntilterminated; RunOnceId: "KillApp"
Filename: "{app}\{#AppExe}"; Parameters: "--unregister-host"; Flags: runhidden waituntilterminated; RunOnceId: "UnregHost"

[Code]
function RelaunchAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

{ Ask a running copy (any version, from any folder) to exit before files are replaced. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
  OldExe: String;
begin
  Result := '';
  OldExe := ExpandConstant('{app}\{#AppExe}');
  if FileExists(OldExe) then
  begin
    { Graceful close (saves state, resumes downloads after the update). Don't wait on it:
      very old builds don't know --quit. taskkill below makes sure nothing is left running. }
    Exec(OldExe, '--quit', '', SW_HIDE, ewNoWait, Code);
    Sleep(4000);
  end;
  Exec(ExpandConstant('{cmd}'), '/C taskkill /F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { "Start with Windows" may also have been switched on from Options }
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#AppName}');
    DataDir := ExpandConstant('{userappdata}\{#AppName}');
    if DirExists(DataDir) and (not UninstallSilent) then
      if MsgBox('Also remove your OpenDownloader settings, download list and the downloaded ' +
                'helper tools (ffmpeg, yt-dlp)?' + #13#10#13#10 +
                'Files you downloaded are NOT touched either way.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
