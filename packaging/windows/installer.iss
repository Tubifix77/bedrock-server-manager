; Inno Setup script for Bedrock Server Manager.
; Build the PyInstaller bundle first (dist\BedrockServerManager\), then:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\windows\installer.iss
; CI overrides the version:
;   ISCC.exe /DMyAppVersion=1.2.3 packaging\windows\installer.iss

#ifndef MyAppVersion
  #define MyAppVersion "1.0.2"
#endif
#define MyAppName "Bedrock Server Manager"
#define MyAppPublisher "Tue Wincentz Boas"
#define MyAppExeName "BedrockServerManager.exe"
#define RepoRoot SourcePath + "..\..\"

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; User picks the folder on the wizard's "Select Destination Location" page (install anywhere).
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
; lowest = per-user install, no admin needed; user may still elevate via the dialog.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#RepoRoot}dist_installer
OutputBaseFilename=BedrockServerManager-{#MyAppVersion}-Setup
SetupIconFile={#RepoRoot}minecraft.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#RepoRoot}dist\BedrockServerManager\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
