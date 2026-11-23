; Instalador do Alerta2UI (Inno Setup 6)
; Gerar: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
; Antes, gere o executável: python -m PyInstaller Alerta2UI.spec --noconfirm

#define AppName "Alerta2UI"
#define AppVersion "1.0.0"
#define AppExe "Alerta2UI.exe"

[Setup]
; AppId identifica o programa para atualizações/desinstalação. Não altere.
AppId={{87D1FEA5-F67D-4739-B21F-885287CE8D1B}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
; {autopf} = C:\Program Files (64 bits)
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=instalador
OutputBaseFilename=Alerta2UI_Setup_{#AppVersion}
SetupIconFile=assets\icone.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=no

[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "startup"; Description: "Iniciar o Alerta2UI junto com o Windows (recomendado)"; GroupDescription: "Opções:"
Name: "desktopicon"; Description: "Criar atalho na Área de Trabalho"; GroupDescription: "Opções:"; Flags: unchecked

[Files]
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Desinstalar {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; "Iniciar com o Windows" é gravado no registro do usuário que está instalando (não do administrador),
; no mesmo lugar que a opção da tela de Configurações usa.
Filename: "{sys}\reg.exe"; Parameters: "add ""HKCU\Software\Microsoft\Windows\CurrentVersion\Run"" /v ""{#AppName}"" /t REG_SZ /d ""\""{app}\{#AppExe}\"""" /f"; Flags: runhidden runasoriginaluser; Tasks: startup
Filename: "{app}\{#AppExe}"; Description: "Iniciar o {#AppName} agora"; Flags: nowait postinstall runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "FecharApp"
Filename: "{sys}\reg.exe"; Parameters: "delete ""HKCU\Software\Microsoft\Windows\CurrentVersion\Run"" /v ""{#AppName}"" /f"; Flags: runhidden; RunOnceId: "RemoverStartup"

[Code]
// Ao atualizar, fecha a versão em execução (ela fica na bandeja) para poder substituir o .exe.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Codigo: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Codigo);
  Sleep(800);
  Result := '';
end;

// As configurações ficam em %APPDATA%\Alerta2UI (criptografadas) e são mantidas ao desinstalar,
// para não perder os dispositivos cadastrados numa reinstalação.
