; LSTtranslate 安装脚本（需求 INS-01 至 INS-07，架构文档第 15 章）。
; 由 scripts\build_installer.py 调用 ISCC 编译，命令行传入 AppVersion、StageDir、OutputDir。
;
; 当前用户安装，不需要管理员权限：{autopf} 在 PrivilegesRequired=lowest 时是 %LOCALAPPDATA%\Programs。
; 程序运行时持有命名互斥体 LSTtranslate.Running，安装、升级、卸载时据此提示用户关闭程序。
; 卸载时询问是否删除用户数据（%LOCALAPPDATA%\LSTtranslate 与凭据管理器中的 API key）；
; 静默卸载默认保留，命令行加 /DELETEUSERDATA=yes 时删除。

#define AppName "LSTtranslate"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\build\stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
; AppId 用来识别同一个软件，升级时据此覆盖安装，发布后不能再改。
AppId={{435CC09B-5F36-4944-9918-62F7215D2057}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=LSTtranslate
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 11 22H2（build 22621）起才有 Mica 背景（需求 6.6 节）。
MinVersion=10.0.22621
AppMutex=LSTtranslate.Running
CloseApplications=yes
RestartApplications=no
OutputDir={#OutputDir}
OutputBaseFilename=LSTtranslate-{#AppVersion}-setup
SetupIconFile=..\assets\icons\lsttranslate.ico
UninstallDisplayIcon={app}\app\assets\icons\lsttranslate.ico
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
; ISCC 是 32 位程序，ultra64 的大字典在进程内压缩会内存不足；在单独的 64 位进程里压缩。
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=4
WizardStyle=modern
ShowLanguageDialog=auto

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
english.AutoStart=Start LSTtranslate when I sign in to Windows
english.AutoStartGroup=Startup:
english.DeleteUserData=Also delete your LSTtranslate data?%n%nThis removes settings, translation cache, statistics, logs, downloaded models and the API keys saved in Windows Credential Manager.%n%n删除 LSTtranslate 的用户数据吗？这会删除配置、译文缓存、统计、日志、已下载的模型，以及保存在 Windows 凭据管理器中的 API key。%n%nChoose No to keep them for a later reinstall. / 选“否”保留，重新安装后继续使用。

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "{cm:AutoStart}"; GroupDescription: "{cm:AutoStartGroup}"; Flags: unchecked

[InstallDelete]
; 升级前删掉旧版本的程序文件，避免残留已经移除的模块；用户数据不在安装目录里，不受影响（需求 INS-07）。
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\site"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\launcher"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -X utf8 ""{app}\launcher\lst_main.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\app\assets\icons\lsttranslate.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -X utf8 ""{app}\launcher\lst_main.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\app\assets\icons\lsttranslate.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\runtime\pythonw.exe"" -I -X utf8 ""{app}\launcher\lst_main.pyw"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -X utf8 ""{app}\launcher\lst_main.pyw"""; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 运行时生成的 __pycache__ 等不在安装记录里，整体删除程序目录。
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\site"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\launcher"
Type: dirifempty; Name: "{app}"

[Code]
var
  DeleteUserData: Boolean;

function UserDataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\{#AppName}');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    if UninstallSilent then
      DeleteUserData := CompareText(ExpandConstant('{param:DELETEUSERDATA|no}'), 'yes') = 0
    else
      DeleteUserData := MsgBox(CustomMessage('DeleteUserData'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
    if DeleteUserData then
    begin
      { 程序文件还在，用安装目录里的解释器清除凭据管理器中的 API key。 }
      if Exec(ExpandConstant('{app}\runtime\python.exe'),
              '-I -X utf8 "' + ExpandConstant('{app}\launcher\worker.py') + '" purge-secrets',
              ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
        Log(Format('purge-secrets exit code: %d', [ResultCode]))
      else
        Log(Format('purge-secrets failed to start: %s', [SysErrorMessage(ResultCode)]));
    end;
  end;
  if (CurUninstallStep = usPostUninstall) and DeleteUserData then
    DelTree(UserDataDir(), True, True, True);
end;
