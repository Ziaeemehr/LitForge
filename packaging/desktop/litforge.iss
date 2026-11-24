; Windows per-user installer (Inno Setup 6). scripts/build_desktop.py passes /DVersion, /DPayload and /DOutput.
; No administrator rights: the program goes to %LOCALAPPDATA%\Programs\LitForge, the runtime it builds during setup
; to %LOCALAPPDATA%\LitForge\runtime. The library is never inside either folder and uninstall does not touch it.

[Setup]
AppId={{6B0D3C5E-1F2A-4C7B-9E4D-7A1B2C3D4E5F}
AppName=LitForge
AppVersion={#Version}
AppPublisher=LitForge
AppPublisherURL=https://github.com/Ziaeemehr/LitForge
DefaultDirName={localappdata}\Programs\LitForge
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#Output}
OutputBaseFilename=LitForge-{#Version}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
LicenseFile={#Payload}\app\LICENSE
WizardStyle=modern
CloseApplications=yes

[Files]
Source: "{#Payload}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{userprograms}\LitForge"; Filename: "{localappdata}\LitForge\runtime\venv\Scripts\litforge-app.exe"
Name: "{userdesktop}\LitForge"; Filename: "{localappdata}\LitForge\runtime\venv\Scripts\litforge-app.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"

[Run]
Filename: "{localappdata}\LitForge\runtime\venv\Scripts\litforge-app.exe"; Description: "Open LitForge"; \
  Flags: postinstall nowait skipifsilent

[UninstallDelete]
; The runtime is program files built by setup (Python, packages, optional tools); the library lives elsewhere.
Type: filesandordirs; Name: "{localappdata}\LitForge\runtime"

[Code]
// Builds or updates the runtime with the bundled uv, in a visible console (uv's progress). Needs internet the first
// time (about 100 MB). A failure stops setup with a message; running the installer again retries.
procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Params: String;
begin
  if CurStep <> ssPostInstall then Exit;
  WizardForm.StatusLabel.Caption := 'Setting up LitForge (downloads Python and its packages)...';
  Params := 'run --no-project --python 3.12 "' + ExpandConstant('{app}\provision.py') + '" --app "' +
    ExpandConstant('{app}\app') + '" --runtime "' + ExpandConstant('{localappdata}\LitForge\runtime') +
    '" --uv "' + ExpandConstant('{app}\bin\uv.exe') + '" --extra desktop';
  if not Exec(ExpandConstant('{app}\bin\uv.exe'), Params, '', SW_SHOW, ewWaitUntilTerminated, Code) or (Code <> 0) then
  begin
    SuppressibleMsgBox('LitForge could not finish its setup (exit code ' + IntToStr(Code) + '). An internet connection is ' +
      'needed the first time. Run this installer again to retry.', mbError, MB_OK, IDOK);  // silent installs must not block
    Abort;
  end;
end;
