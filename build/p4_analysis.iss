; Windows installer for P4 Analysis (Inno Setup). Built by build_installer.py, which passes AppVersion.
; Installs for the current user, so it needs no administrator rights. Downloads and results live in
; %USERPROFILE%\P4 Analysis, outside the program folder, and are kept when the app is uninstalled.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7E3C4C0E-5A0F-4C7B-9D0A-3E7A2C8F1B64}
AppName=P4 Analysis
AppVersion={#AppVersion}
AppVerName=P4 Analysis {#AppVersion}
DefaultDirName={localappdata}\Programs\P4 Analysis
DefaultGroupName=P4 Analysis
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=P4-Analysis-Setup-{#AppVersion}
SetupIconFile=..\p4_app\assets\p4.ico
UninstallDisplayIcon={app}\P4 Analysis.exe
UninstallDisplayName=P4 Analysis
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=8

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "stage\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\P4 Analysis"; Filename: "{app}\P4 Analysis.exe"
Name: "{group}\Uninstall P4 Analysis"; Filename: "{uninstallexe}"
Name: "{autodesktop}\P4 Analysis"; Filename: "{app}\P4 Analysis.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\P4 Analysis.exe"; Description: "Open P4 Analysis"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; files Python creates after installation (compiled caches, the pynwb type map)
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\p4_app"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\tests"
