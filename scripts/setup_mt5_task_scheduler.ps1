# setup_mt5_task_scheduler.ps1
#
# ★★★ これは「投資専用PC」上で1回だけ実行するセットアップスクリプト ★★★
# （Claude Codeが操作している開発用PCではなく、MT5端末を常時起動しておく
#   実際のトレード用PC上で、そのPCのPowerShellから直接実行すること）
#
# 事前準備（このスクリプトを実行する前に済ませておくこと）:
#   1. このリポジトリを投資専用PCに git clone しておく
#      （git pull できる認証状態: gh auth login 済み、または git credential 設定済み）
#   2. Python をインストールし、`pip install -r flow_layer/requirements.txt` 等
#      必要な依存関係（numpy/pandas/scipy/requests/MetaTrader5）を入れておく
#      （MetaTrader5パッケージ: pip install MetaTrader5）
#   3. MT5端末をインストールし、XM(XMTrading)デモ/実口座にログインしておく
#      （AutoTradingボタンをON、Ctrl+E）
#   4. メール通知を使うならSMTP認証情報を「永続的な」環境変数として設定
#      （下のコマンド例のUSER欄を書き換えて一度だけ実行）:
#        [Environment]::SetEnvironmentVariable("SMTP_USER","xxx@gmail.com","User")
#        [Environment]::SetEnvironmentVariable("SMTP_PASS","アプリパスワード","User")
#        [Environment]::SetEnvironmentVariable("MAIL_FROM","xxx@gmail.com","User")
#        [Environment]::SetEnvironmentVariable("MAIL_TO","通知を受け取るアドレス","User")
#      設定後はPowerShellを開き直すか、後述のタスク再登録が必要
#
# このスクリプトがやること:
#   - python.exe のフルパスを自動検出
#   - クラウド側の指値スキャン（07:00/13:00/21:00 JST）から15分後の
#     07:15/13:15/21:15 JSTに scripts/run_mt5_executor.ps1 を実行するタスクを登録
#     （クラウド側のGitHub Actionsがpending_orders.jsonをpushし終える猶予を見込む）
#   - タスクは現在ログイン中のユーザーで、PCがスリープしていない限り実行される
#     （スリープ復帰時にも走らせたい場合は後述の電源設定を別途調整すること）

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$wrapperScript = Join-Path $repoRoot "scripts\run_mt5_executor.ps1"

if (-not (Test-Path $wrapperScript)) {
    Write-Error "run_mt5_executor.ps1 が見つかりません。このスクリプトはリポジトリ内のscripts/から実行してください。"
    exit 1
}

$pythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonPath) {
    Write-Error "python が見つかりません。事前準備2を済ませてから再実行してください。"
    exit 1
}
Write-Output "検出したpython: $pythonPath"

$taskName = "FX-MT5-LocalExecutor"
$powershellExe = (Get-Command powershell.exe).Source

$action = New-ScheduledTaskAction -Execute $powershellExe `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$wrapperScript`""

$triggers = @(
    New-ScheduledTaskTrigger -Daily -At "07:15"
    New-ScheduledTaskTrigger -Daily -At "13:15"
    New-ScheduledTaskTrigger -Daily -At "21:15"
)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal -Description `
    "fx-signal-monitor: クラウド側の指値待機シグナルをMT5へ自動発注（1日3回）" `
    -Force

Write-Output ""
Write-Output "タスク '$taskName' を登録しました。1日3回（07:15/13:15/21:15 JST）に"
Write-Output "$wrapperScript を実行します。"
Write-Output ""
Write-Output "確認方法: タスクスケジューラー(taskschd.msc)を開いて '$taskName' を探すか、"
Write-Output "以下のコマンドで手動テスト実行できます:"
Write-Output "  Start-ScheduledTask -TaskName '$taskName'"
Write-Output "実行後、$repoRoot\logs\ 配下のログファイルで結果を確認してください。"
