; Установщик SDR4NOOB (Inno Setup 6). Собирается из готовой папки dist\SDR4NOOB (build.bat):
;   iscc /DAppVersion=3.1.0 installer\sdr4noob.iss      → release\SDR4NOOB-3.1.0-setup.exe
;
; Мастер: папка программы → папка для файлов пользователя (записи, снимки, звук, журналы) → автообновление.
; Выбор записывается в настройки программы вызовом «SDR4NOOB.exe --configure …» (см. rtltv/__main__.py).
; По умолчанию — установка для текущего пользователя без прав администратора (%LOCALAPPDATA%\Programs):
; так автообновление может само заменять файлы программы.
;
; Тихая установка: SDR4NOOB-x-setup.exe /VERYSILENT [/DIR="…"] [/DATADIR="…"] [/UPDATES=0|1] [/LAUNCH=1]
; (DATADIR и UPDATES по умолчанию — как в текущих настройках; LAUNCH=1 — запустить программу после установки,
; так её перезапускает автообновление).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "SDR4NOOB"
#define AppExe "SDR4NOOB.exe"
#define AppUrl "https://mike-ileech.github.io/SDR4NOOB/"
#define RepoUrl "https://github.com/Mike-iLeech/SDR4NOOB"

[Setup]
AppId={{6B8E2F4A-5C1D-4E7B-9A3F-2D4C8B1E7F05}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Mike-iLeech
AppPublisherURL={#AppUrl}
AppSupportURL={#RepoUrl}/issues
AppUpdatesURL={#RepoUrl}/releases/latest
AppComments=Всё в одном для начинающего радиолюбителя
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename={#AppName}-{#AppVersion}-setup
SetupIconFile=..\rtltv\assets\sdr4noob.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
LicenseFile=..\LICENSE
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} — установка

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Ярлыки:"

[InstallDelete]
; старые файлы программы, чтобы от прошлой версии не оставалось лишнего
Type: filesandordirs; Name: "{app}\_internal"
; прежнее имя программы (до 3.0 она называлась RtlTV)
Type: files; Name: "{app}\RtlTV.exe"
Type: files; Name: "{app}\RtlTV.exe.old"

[Files]
Source: "..\dist\SDR4NOOB\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Всё в одном для начинающего радиолюбителя"
Name: "{group}\Руководство и сайт {#AppName}"; Filename: "{#AppUrl}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; записать выбор мастера в настройки программы (окон не открывает)
Filename: "{app}\{#AppExe}"; Parameters: "--configure --data-dir ""{code:GetDataDir}"" --updates {code:GetUpdates}"; \
  StatusMsg: "Сохранение настроек…"; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "Запустить {#AppName}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: LaunchAfterSilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Code]
var
  DataPage: TInputDirWizardPage;
  UpdPage: TInputOptionWizardPage;

{ Значение строкового ключа "key": "…" из app.json (без полного разбора JSON; null и отсутствие → '') }
function JsonStr(const Json, Key: String): String;
var
  P, I: Integer;
  S: String;
begin
  Result := '';
  P := Pos('"' + Key + '"', Json);
  if P = 0 then exit;
  S := Copy(Json, P + Length(Key) + 2, Length(Json));
  P := Pos(':', S);
  if P = 0 then exit;
  S := TrimLeft(Copy(S, P + 1, Length(S)));
  if (Length(S) = 0) or (S[1] <> '"') then exit;
  I := 2;
  while I <= Length(S) do
  begin
    if S[I] = '\' then
    begin
      if I < Length(S) then Result := Result + S[I + 1];
      I := I + 2;
    end
    else if S[I] = '"' then
      break
    else
    begin
      Result := Result + S[I];
      I := I + 1;
    end;
  end;
end;

{ app.json записан в UTF-8 (кириллица в путях) — декодируем байты сами }
function Utf8ToStr(const A: AnsiString): String;
var
  I, B, C, N: Integer;
begin
  Result := '';
  I := 1;
  while I <= Length(A) do
  begin
    B := Ord(A[I]);
    if B < $80 then begin C := B; N := 0; end
    else if (B and $E0) = $C0 then begin C := B and $1F; N := 1; end
    else if (B and $F0) = $E0 then begin C := B and $0F; N := 2; end
    else begin C := Ord('?'); N := 3; end;
    while (N > 0) and (I < Length(A)) do
    begin
      I := I + 1;
      C := (C shl 6) or (Ord(A[I]) and $3F);
      N := N - 1;
    end;
    if C > $FFFF then C := Ord('?');
    Result := Result + Chr(C);
    I := I + 1;
  end;
end;

function ReadSettings(): String;
var
  A: AnsiString;
begin
  Result := '';
  if LoadStringFromFile(ExpandConstant('{userappdata}\SDR4NOOB\app.json'), A) then
    Result := Utf8ToStr(A)
  else if LoadStringFromFile(ExpandConstant('{userappdata}\RtlTV\app.json'), A) then
    Result := Utf8ToStr(A);
end;

procedure InitializeWizard;
var
  Json, Data, Upd: String;
begin
  Json := ReadSettings();
  Data := JsonStr(Json, 'data_dir');
  if Data = '' then
  begin
    { прежняя версия (RtlTV) по умолчанию писала в «Документы\RtlTV» — если там уже что-то есть, остаёмся там }
    if DirExists(ExpandConstant('{userdocs}\RtlTV')) and not DirExists(ExpandConstant('{userdocs}\SDR4NOOB')) then
      Data := ExpandConstant('{userdocs}\RtlTV')
    else
      Data := ExpandConstant('{userdocs}\SDR4NOOB');
  end;
  if ExpandConstant('{param:DATADIR|}') <> '' then
    Data := ExpandConstant('{param:DATADIR|}');
  Upd := JsonStr(Json, 'update_check');

  DataPage := CreateInputDirPage(wpSelectDir,
    'Папка для ваших файлов',
    'Куда SDR4NOOB будет сохранять всё, что вы примете?',
    'Здесь будут записи пролётов спутников (до 2 ГБ за пролёт), снимки, записи звука и журналы. ' +
    'Удобно выбрать большой второй диск. Папку можно сменить позже в «Настройках» программы.' + #13#10#13#10 +
    'При удалении программы эта папка не стирается.',
    False, 'SDR4NOOB');
  DataPage.Add('Папка данных:');
  DataPage.Values[0] := Data;

  UpdPage := CreateInputOptionPage(DataPage.ID,
    'Обновления',
    'Проверять новые версии автоматически?',
    'SDR4NOOB может раз в день спрашивать у GitHub, не вышла ли новая версия, и предлагать обновиться ' +
    '(ничего не ставится без вашего согласия). Настройка меняется в любой момент: «Настройки» → «Обновления».',
    False, False);
  UpdPage.Add('Автоматически проверять обновления (раз в день)');
  if ExpandConstant('{param:UPDATES|}') <> '' then
    UpdPage.Values[0] := ExpandConstant('{param:UPDATES|1}') <> '0'
  else
    UpdPage.Values[0] := Upd <> 'never';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = DataPage.ID) and (Trim(DataPage.Values[0]) = '') then
  begin
    MsgBox('Укажите папку для ваших файлов.', mbError, MB_OK);
    Result := False;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
var
  U: String;
begin
  if UpdPage.Values[0] then U := 'проверять раз в день' else U := 'не проверять';
  Result := MemoDirInfo + NewLine + NewLine +
    'Папка для ваших файлов:' + NewLine + Space + DataPage.Values[0] + NewLine + NewLine +
    'Обновления:' + NewLine + Space + U;
  if MemoTasksInfo <> '' then Result := Result + NewLine + NewLine + MemoTasksInfo;
end;

function GetDataDir(Param: String): String;
begin
  Result := Trim(DataPage.Values[0]);
  { «D:\» в кавычках экранирует кавычку в командной строке — «D:\.» безопасно и означает то же }
  if (Length(Result) > 0) and (Result[Length(Result)] = '\') then
    Result := Result + '.';
end;

function GetUpdates(Param: String): String;
begin
  if UpdPage.Values[0] then Result := 'daily' else Result := 'never';
end;

function LaunchAfterSilent(): Boolean;
begin
  Result := WizardSilent() and (ExpandConstant('{param:LAUNCH|0}') = '1');
end;
