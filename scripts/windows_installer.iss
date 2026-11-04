#ifndef AppVersion
  #error AppVersion must be supplied
#endif
#ifndef BundleDir
  #error BundleDir must be supplied
#endif
#ifndef OutputDir
  #error OutputDir must be supplied
#endif

[Setup]
AppId={{8FC97F07-7309-41B3-9F9C-3D1AD90770BC}
AppName=Lumina Pendant Prep
AppVersion={#AppVersion}
AppPublisher=Lumina Pendant Prep contributors
AppPublisherURL=https://github.com/FredMuir/Lumina-Pendant-Prep
DefaultDirName={localappdata}\Programs\LuminaPendantPrep
DefaultGroupName=Lumina Pendant Prep
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=LuminaPendantPrep-{#AppVersion}-Windows-x64-Setup
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\LuminaPendantPrep.exe

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Lumina Pendant Prep"; Filename: "{app}\LuminaPendantPrep.exe"
Name: "{autodesktop}\Lumina Pendant Prep (Installed)"; Filename: "{app}\LuminaPendantPrep.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\LuminaPendantPrep.exe"; Description: "Launch Lumina Pendant Prep"; Flags: nowait postinstall skipifsilent
