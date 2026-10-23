; LSTtranslate 安装脚本（需求 INS-01 至 INS-07，架构文档第 15 章）。
; 由 scripts\build_installer.py 调用 ISCC 编译，命令行传入 AppVersion、StageDir、OutputDir、Flavor、MarkerDir。
;
; 构建类型（需求 DEV-02、DEV-08，架构文档 16.6 节）：Flavor 是 normal（普通版）或 dev（开发版）。两种安装包的
; 程序文件相同，AppId 也相同，互相覆盖安装，安装后的启动方式相同；区别只有 {app}\app\installed.json：
; 暂存目录里那份是构建脚本自检用的，这里排除它，改装 MarkerDir 下按 Flavor 写好的 installed.json
; （flavor 字段决定自动化接口的默认开关与应用内更新下载哪一种安装包）。开发版的安装包名多一段 -dev：
; LSTtranslate-<版本>-dev-setup.exe。
;
; 当前用户安装，不需要管理员权限：{autopf} 在 PrivilegesRequired=lowest 时是 %LOCALAPPDATA%\Programs。
; 程序运行时持有命名互斥体 LSTtranslate.Running，安装、升级、卸载时据此提示用户关闭程序。
; 卸载时询问是否删除用户数据（数据目录与凭据管理器中的 API key）；
; 静默卸载默认保留，命令行加 /DELETEUSERDATA=yes 时删除。
; 数据目录默认是 %LOCALAPPDATA%\LSTtranslate；用户设置了环境变量 LST_DATA_DIR 时程序改用它指定的目录
; （lst/app/paths.py）。卸载程序以当前用户身份运行，读得到同一个环境变量，两个目录都删。
; LST_DATA_DIR 指定的目录只有在是绝对路径、不是驱动器根目录、并且里面有 config\settings.toml 时才删，
; 免得变量指错地方时删掉别的东西。
; 用户在设置里另选的数据目录记在 %APPDATA%\LSTtranslate\location.json，另选的模型目录记在数据目录的配置里
; （[storage].models_dir）。这两个目录可能还放着用户自己的文件，卸载脚本不整个删除它们，而是运行
; launcher\worker.py purge-data，只删程序创建的条目，再删位置文件（lst/app/uninstall.py 的 purge_user_data）。
; 选“保留用户数据”时位置文件也保留，重新安装后程序继续使用选好的目录。
;
; 应用内更新（lst/services/updates）用 /SILENT /SUPPRESSMSGBOXES /NORESTART /RELAUNCH=yes 启动新安装包，随后程序退出：
; - InitializeSetup 在 AppMutex 检查之前执行（Inno Setup 6 实测）。带 /RELAUNCH=yes 时在这里最多等 120 秒，
;   等程序释放互斥体。超时后放弃这次更新（InitializeSetup 返回 False），旧程序仍在运行；
; - 静默安装且带 /RELAUNCH=yes 时，装完由 [Run] 的最后一项启动程序；
; - 带 /RELAUNCH=yes 但没有装完（等待超时、AppMutex 检查不过、安装出错）时，DeinitializeSetup 在旧程序
;   已经退出的情况下重新启动它，用户不会在更新失败后发现程序消失了。
;
; 安装包不捆绑 PaddleOCR：运行环境与模型由程序按需下载到模型目录（lst/services/ocr_engines），
; tools\uv.exe 用来安装运行环境，app\ocr-paddle-requirements.txt 是它的版本清单。

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
#ifndef Flavor
  #define Flavor "normal"
#endif
#ifndef MarkerDir
  #define MarkerDir "..\build\flavors\" + Flavor
#endif
#if Flavor == "dev"
  #define FlavorSuffix "-dev"
#elif Flavor == "normal"
  #define FlavorSuffix ""
#else
  #error Flavor must be normal or dev
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
; Windows 10 22H2 x64；Mica 在 Windows 11 22H2 起启用，旧系统使用不透明主题背景。
MinVersion=10.0.19045
AppMutex=LSTtranslate.Running
CloseApplications=yes
RestartApplications=no
OutputDir={#OutputDir}
OutputBaseFilename=LSTtranslate-{#AppVersion}{#FlavorSuffix}-setup
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
Type: filesandordirs; Name: "{app}\tools"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Excludes: "\app\installed.json"; Flags: ignoreversion recursesubdirs createallsubdirs
; 安装版标记按构建类型取，见文件头的说明。
Source: "{#MarkerDir}\installed.json"; DestDir: "{app}\app"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -X utf8 ""{app}\launcher\lst_main.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\app\assets\icons\lsttranslate.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -X utf8 ""{app}\launcher\lst_main.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\app\assets\icons\lsttranslate.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\runtime\pythonw.exe"" -I -X utf8 ""{app}\launcher\lst_main.pyw"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -X utf8 ""{app}\launcher\lst_main.pyw"""; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; 应用更新：静默安装且命令行带 /RELAUNCH=yes 时，装完启动新版本。
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -X utf8 ""{app}\launcher\lst_main.pyw"""; WorkingDir: "{app}"; Flags: nowait skipifnotsilent runasoriginaluser; Check: ShouldRelaunch

[UninstallDelete]
; 运行时生成的 __pycache__ 等不在安装记录里，整体删除程序目录。
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\site"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\launcher"
Type: filesandordirs; Name: "{app}\tools"
Type: dirifempty; Name: "{app}"

[Code]
const
  RunningMutex = 'LSTtranslate.Running';
  RelaunchWaitSeconds = 120;
  { 放弃更新后，再给旧程序这么久退出，然后判断要不要把它启动回来。 }
  RestartWaitSeconds = 10;
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{435CC09B-5F36-4944-9918-62F7215D2057}_is1';

var
  DeleteUserData: Boolean;
  InstallFinished: Boolean;

function RelaunchRequested(): Boolean;
begin
  Result := CompareText(ExpandConstant('{param:RELAUNCH|no}'), 'yes') = 0;
end;

function ShouldRelaunch(): Boolean;
begin
  Result := WizardSilent and RelaunchRequested;
end;

function WaitForMutexRelease(Seconds: Integer): Boolean;
var
  Waited: Integer;
begin
  Waited := 0;
  while CheckForMutexes(RunningMutex) and (Waited < Seconds * 4) do
  begin
    Sleep(250);
    Waited := Waited + 1;
  end;
  Result := not CheckForMutexes(RunningMutex);
  Log(Format('Waited %d ms for %s; released: %d', [Waited * 250, RunningMutex, Ord(Result)]));
end;

function InitializeSetup(): Boolean;
begin
  InstallFinished := False;
  Result := True;
  { 应用更新时程序在启动安装包之后才退出。这里等它释放互斥体，避免 AppMutex 检查让静默安装直接取消。 }
  if RelaunchRequested and not WaitForMutexRelease(RelaunchWaitSeconds) then
  begin
    Log('LSTtranslate is still running; update abandoned');
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssDone then
    InstallFinished := True;
end;

function InstalledAppDir(var Dir: String): Boolean;
begin
  { 当前用户安装（PrivilegesRequired=lowest）时卸载信息登记在 HKCU。 }
  Result := RegQueryStringValue(HKCU, UninstallKey, 'Inno Setup: App Path', Dir) and (Dir <> '');
end;

procedure DeinitializeSetup();
var
  Dir, Pythonw: String;
  ResultCode: Integer;
begin
  { 只处理应用内更新没有装完的情况；装完时由 [Run] 启动新版本。 }
  if not RelaunchRequested or InstallFinished then
    Exit;
  if not WaitForMutexRelease(RestartWaitSeconds) then
  begin
    Log('Update not finished; LSTtranslate is still running, nothing to restart');
    Exit;
  end;
  if not InstalledAppDir(Dir) then
  begin
    Log('Update not finished; previous installation not found, cannot restart');
    Exit;
  end;
  Pythonw := AddBackslash(Dir) + 'runtime\pythonw.exe';
  if not FileExists(Pythonw) then
  begin
    Log('Update not finished; ' + Pythonw + ' is missing, cannot restart');
    Exit;
  end;
  if Exec(Pythonw, '-I -X utf8 "' + AddBackslash(Dir) + 'launcher\lst_main.pyw"', Dir, SW_SHOWNORMAL, ewNoWait, ResultCode) then
    Log('Update not finished; restarted the previous LSTtranslate')
  else
    Log(Format('Update not finished; failed to restart LSTtranslate: %s', [SysErrorMessage(ResultCode)]));
end;

function UserDataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\{#AppName}');
end;

{ 环境变量 LST_DATA_DIR 指定的数据目录。只接受像是本程序数据目录的绝对路径，否则返回空串。 }
function CustomDataDir(): String;
var
  Dir: String;
begin
  Result := '';
  Dir := RemoveBackslashUnlessRoot(Trim(GetEnv('LST_DATA_DIR')));
  if Dir = '' then
    Exit;
  if not ((Length(Dir) > 3) and (Copy(Dir, 2, 2) = ':\')) and not (Copy(Dir, 1, 2) = '\\') then
  begin
    Log('LST_DATA_DIR is not an absolute directory path; ignored');
    Exit;
  end;
  if not FileExists(AddBackslash(Dir) + 'config\settings.toml') then
  begin
    Log('LST_DATA_DIR has no config\settings.toml; not deleted');
    Exit;
  end;
  Result := Dir;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  CustomDir: String;
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
        { 退出码非 0 表示有凭据没删掉（launcher\worker.py 的 EXIT_PURGE_FAILED），只记日志，不阻止卸载。 }
        Log(Format('purge-secrets exit code: %d', [ResultCode]))
      else
        Log(Format('purge-secrets failed to start: %s', [SysErrorMessage(ResultCode)]));
      { 设置里另选的数据目录与模型目录：只删程序创建的条目（launcher\worker.py 的 purge-data）。 }
      if Exec(ExpandConstant('{app}\runtime\python.exe'),
              '-I -X utf8 "' + ExpandConstant('{app}\launcher\worker.py') + '" purge-data',
              ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
        Log(Format('purge-data exit code: %d', [ResultCode]))
      else
        Log(Format('purge-data failed to start: %s', [SysErrorMessage(ResultCode)]));
    end;
  end;
  if (CurUninstallStep = usPostUninstall) and DeleteUserData then
  begin
    DelTree(UserDataDir(), True, True, True);
    { 位置文件所在的目录只放本程序的位置文件；purge-data 没能删掉时在这里补删。 }
    DelTree(ExpandConstant('{userappdata}\{#AppName}'), True, True, True);
    CustomDir := CustomDataDir();
    if (CustomDir <> '') and (CompareText(CustomDir, UserDataDir()) <> 0) then
    begin
      Log('Deleting data directory from LST_DATA_DIR');
      DelTree(CustomDir, True, True, True);
    end;
  end;
end;
