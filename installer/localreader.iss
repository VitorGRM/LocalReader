; Instalador do LocalReader (Inno Setup 6). Compilado por build_windows.ps1:
;   ISCC /DAppVersion=1.2.3 installer\localreader.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Nunca mude o AppId: é ele que faz o instalador atualizar a instalação existente
; (inclusive as de quando o programa se chamava TTS Reader, que continuam na pasta antiga).
AppId={{B6F0C1A4-3D2E-4E7B-9C58-7A1D2F4E8B31}
AppName=LocalReader
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
; Instalação por usuário (%LOCALAPPDATA%\Programs): não pede administrador,
; então o atualizador automático também não precisa de UAC.
PrivilegesRequired=lowest
DefaultDirName={autopf}\LocalReader
DefaultGroupName=LocalReader
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=LocalReader-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\LocalReader.exe
CloseApplications=yes
RestartApplications=no
#if FileExists("..\assets\icon.ico")
SetupIconFile=..\assets\icon.ico
#endif

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar um atalho na Área de Trabalho"; Flags: unchecked

[Files]
Source: "..\dist\LocalReader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Até a v1.5.x o programa se chamava TTS Reader: tira o executável e os atalhos antigos.
Type: files; Name: "{app}\TTSReader.exe"
Type: files; Name: "{autoprograms}\TTS Reader.lnk"
Type: files; Name: "{autodesktop}\TTS Reader.lnk"

[Icons]
Name: "{autoprograms}\LocalReader"; Filename: "{app}\LocalReader.exe"
Name: "{autodesktop}\LocalReader"; Filename: "{app}\LocalReader.exe"; Tasks: desktopicon

[Run]
; Instalação manual: caixa "Abrir LocalReader" ao final.
Filename: "{app}\LocalReader.exe"; Description: "Abrir o LocalReader"; Flags: nowait postinstall skipifsilent
; Atualização automática (o app passa /RELAUNCH=1): reabre sozinho.
Filename: "{app}\LocalReader.exe"; Flags: nowait; Check: RelaunchRequested

[Code]
function RelaunchRequested: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;
