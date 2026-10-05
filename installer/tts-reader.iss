; Instalador do TTS Reader (Inno Setup 6). Compilado por build_windows.ps1:
;   ISCC /DAppVersion=1.2.3 installer\tts-reader.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Nunca mude o AppId: é ele que faz o instalador atualizar a instalação existente.
AppId={{B6F0C1A4-3D2E-4E7B-9C58-7A1D2F4E8B31}
AppName=TTS Reader
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
; Instalação por usuário (%LOCALAPPDATA%\Programs): não pede administrador,
; então o atualizador automático também não precisa de UAC.
PrivilegesRequired=lowest
DefaultDirName={autopf}\TTS Reader
DefaultGroupName=TTS Reader
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=TTSReader-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\TTSReader.exe
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
Source: "..\dist\TTSReader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\TTS Reader"; Filename: "{app}\TTSReader.exe"
Name: "{autodesktop}\TTS Reader"; Filename: "{app}\TTSReader.exe"; Tasks: desktopicon

[Run]
; Instalação manual: caixa "Abrir TTS Reader" ao final.
Filename: "{app}\TTSReader.exe"; Description: "Abrir o TTS Reader"; Flags: nowait postinstall skipifsilent
; Atualização automática (o app passa /RELAUNCH=1): reabre sozinho.
Filename: "{app}\TTSReader.exe"; Flags: nowait; Check: RelaunchRequested

[Code]
function RelaunchRequested: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;
