; Inno Setup script for the Keystone agent MSI/EXE installer.
;
; Build with Inno Setup Compiler (ISCC.exe):
;   iscc.exe installer/keystone-agent.iss
;
; Input:  installer/dist/KeystoneAgent/ (from PyInstaller)
; Output: installer/output/KeystoneAgentSetup_<version>.exe
;
; Phase 0 scaffold. The installer:
;   - lays down files under %ProgramFiles%\Keystone\Agent\
;   - registers the Windows service (KeystoneAgent)
;   - opens the first-run wizard in the default browser on install finish
;   - adds an Uninstall entry in Add/Remove Programs
;
; Phase 4 adds: code signing, auto-update hook, silent/unattended flags
; for IT departments, per-user vs machine-wide modes.

#define MyAppName      "Keystone Agent"
#define MyAppVersion   "0.1.0"
#define MyAppPublisher "GoldStandard Wealth Pvt Ltd"
#define MyAppURL       "https://keystone.goldstandardwealth.in"
#define MyAppExeName   "KeystoneAgent.exe"

[Setup]
AppId={{E1B8F22A-8C3A-4C8E-A0C6-2E3C2F63C8B1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\Keystone\Agent
DefaultGroupName=Keystone
DisableProgramGroupPage=yes
OutputDir=output
OutputBaseFilename=KeystoneAgentSetup_{#MyAppVersion}
Compression=lzma
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardStyle=modern
; Phase 4: SignTool=signtool, SignedUninstaller=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\KeystoneAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{commonappdata}\Keystone"; Permissions: users-modify

[Icons]
Name: "{group}\Keystone Agent"; Filename: "http://127.0.0.1:5001/"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall Keystone Agent"; Filename: "{uninstallexe}"

[Run]
; Register and start the service after file install
Filename: "{app}\{#MyAppExeName}"; Parameters: "install"; Flags: runhidden waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Parameters: "start"; Flags: runhidden waituntilterminated
; Open the local UI in the default browser
Filename: "http://127.0.0.1:5001/"; Description: "Open Keystone Agent"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "stop"; Flags: runhidden waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Parameters: "remove"; Flags: runhidden waituntilterminated

[Code]
// Placeholder for Phase 1 wizard page additions (pairing code entry, etc.)
