# run_mt5_executor.ps1
# タスクスケジューラーから呼ばれるラッパー。
#   1. リポジトリを最新に更新（クラウド側の指値スキャン結果を取り込む）
#   2. scripts/mt5_local_executor.py を実行（MT5端末が起動・ログイン済み前提）
#   3. 標準出力/エラーを logs/ 配下にタイムスタンプ付きで記録
#      （Task Schedulerはウィンドウ非表示で動くため、ログを見ないと結果が分からない）
#
# 前提: このPC上で
#   - MT5端末が常時起動し、XM口座にログイン済みであること
#   - git clone 済みで、このリポジトリの認証情報（git pull できる状態）が整っていること
#   - python が使えること（setup_mt5_task_scheduler.ps1 実行時にフルパスを自動検出）
#   - メール通知を使うなら SMTP_USER/SMTP_PASS/MAIL_FROM/MAIL_TO を
#     [Environment]::SetEnvironmentVariable(...,"User") 等で永続化しておくこと
#     （Task Schedulerの実行プロセスは対話シェルの一時的な $env: を引き継がないため）

$ErrorActionPreference = "Continue"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$logDir = Join-Path $repoRoot "logs"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}
$stamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$logFile = Join-Path $logDir "mt5_executor_$stamp.log"

"=== $stamp 開始 ===" | Out-File -FilePath $logFile -Encoding utf8

try {
    $pullOutput = git pull --rebase origin main 2>&1
    $pullOutput | Out-File -FilePath $logFile -Append -Encoding utf8
} catch {
    "git pull失敗: $_" | Out-File -FilePath $logFile -Append -Encoding utf8
}

try {
    $runOutput = python scripts\mt5_local_executor.py 2>&1
    $runOutput | Out-File -FilePath $logFile -Append -Encoding utf8
} catch {
    "mt5_local_executor.py実行失敗: $_" | Out-File -FilePath $logFile -Append -Encoding utf8
}

"=== $(Get-Date -Format 'yyyy-MM-dd_HH-mm-ss') 終了 ===" | Out-File -FilePath $logFile -Append -Encoding utf8
