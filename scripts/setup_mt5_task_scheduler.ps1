# setup_mt5_task_scheduler.ps1
#
# ★★★ これは「投資専用PC」上で1回だけ実行するセットアップスクリプト ★★★
# （Claude Codeが操作している開発用PCではなく、MT5端末を常時起動しておく
#   実際のトレード用PC上で、そのPCのPowerShellから直接実行すること。
#   ただし投資専用PCが用意できるまでの間は、開発用PCで動作確認のために
#   一時的に実行してもよい。）
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
# このスクリプトが登録する2つのタスク:
#   1. FX-MT5-LocalExecutor（1日3回・07:15/13:15/21:15 JST）
#      クラウド側の指値待機シグナル(pending_orders.json)を読み、未発注分を
#      自動発注する。クラウド側スキャン(07:00/13:00/21:00)の15分後に設定し、
#      GitHub Actions側のpush完了を待つ猶予を持たせている。
#   2. FX-MT5-PositionManager（毎時5分・実運用の要）
#      保有中の実ポジションを再評価し、TP到達後のSL移動・トレーリング・
#      SIGNAL_LOST/REVERSED判定による決済を行う。クラウド側の毎時スキャンと
#      同じ頻度で回す必要がある（トレーリングは価格変動への追随が命なので、
#      1日3回では粗すぎる）。

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$wrapperScript = Join-Path $repoRoot "scripts\run_mt5_script.ps1"

if (-not (Test-Path $wrapperScript)) {
    Write-Error "run_mt5_script.ps1 が見つかりません。このスクリプトはリポジトリ内のscripts/から実行してください。"
    exit 1
}

$pythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonPath) {
    Write-Error "python が見つかりません。事前準備2を済ませてから再実行してください。"
    exit 1
}
Write-Output "検出したpython: $pythonPath"

$powershellExe = (Get-Command powershell.exe).Source
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)

# ── タスク1: 発注（1日3回） ──────────────────────────────
# 2026-07-23判明: 複数行@()配列+バッククォート改行継続の組み合わせで
# Register-ScheduledTaskのパラメータ解釈が実機で崩れる事象を確認したため、
# 単一行の配列構築+パラメータスプラッティング(@hashtable)に統一している。
$executorArg = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $wrapperScript, "-TargetScript", "scripts\mt5_local_executor.py") -join " "
$executorAction = New-ScheduledTaskAction -Execute $powershellExe -Argument $executorArg
$executorTriggers = @((New-ScheduledTaskTrigger -Daily -At "07:15"), (New-ScheduledTaskTrigger -Daily -At "13:15"), (New-ScheduledTaskTrigger -Daily -At "21:15"))
$executorTaskParams = @{
    TaskName    = "FX-MT5-LocalExecutor"
    Action      = $executorAction
    Trigger     = $executorTriggers
    Settings    = $settings
    Principal   = $principal
    Description = "fx-signal-monitor: 指値待機シグナルをMT5へ自動発注（1日3回）"
    Force       = $true
}
Register-ScheduledTask @executorTaskParams | Out-Null
Write-Output "タスク 'FX-MT5-LocalExecutor' を登録（07:15/13:15/21:15 JST）"

# ── タスク2: ポジション管理（毎時） ────────────────────────
$pmArg = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $wrapperScript, "-TargetScript", "scripts\mt5_position_manager.py") -join " "
$pmAction = New-ScheduledTaskAction -Execute $powershellExe -Argument $pmArg
# [TimeSpan]::MaxValueはTask SchedulerのXMLが受け付ける範囲外(実機でエラー確認済み)。
# 実用上「無期限」とみなせる10年を使う。
$pmTrigger = New-ScheduledTaskTrigger -Once -At "00:05" -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$pmTaskParams = @{
    TaskName    = "FX-MT5-PositionManager"
    Action      = $pmAction
    Trigger     = $pmTrigger
    Settings    = $settings
    Principal   = $principal
    Description = "fx-signal-monitor: 保有中MT5ポジションのSL/トレーリング/決済判定を毎時実行"
    Force       = $true
}
Register-ScheduledTask @pmTaskParams | Out-Null
Write-Output "タスク 'FX-MT5-PositionManager' を登録（毎時05分）"

Write-Output ""
Write-Output "2つのタスクを登録しました。"
Write-Output ""
Write-Output "確認方法: タスクスケジューラー(taskschd.msc)を開くか、以下で手動テスト実行できます:"
Write-Output "  Start-ScheduledTask -TaskName 'FX-MT5-LocalExecutor'"
Write-Output "  Start-ScheduledTask -TaskName 'FX-MT5-PositionManager'"
Write-Output "実行後、$repoRoot\logs\ 配下のログファイルで結果を確認してください。"

