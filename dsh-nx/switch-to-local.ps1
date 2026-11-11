<#
  把 DSH 里安装的 dsh-nx 换成工作区开发版（junction 软链）。
  用法（先完全退出 DSH 桌面端）：
      powershell -ExecutionPolicy Bypass -File .\switch-to-local.ps1 [开发版目录] [profile名]
  参数都可以省略：
      开发版目录  默认 = 本脚本所在目录（也就是这个包自己）
      profile名   默认 = desktop
#>
$ErrorActionPreference = 'Stop'

$target = if ($args.Count -ge 1 -and $args[0]) { (Resolve-Path -LiteralPath $args[0]).Path }
          else { $PSScriptRoot }
$profileName = if ($args.Count -ge 2 -and $args[1]) { $args[1] } else { 'desktop' }

$profileDir = Join-Path $env:USERPROFILE ('.dsh\profiles\' + $profileName)
$link    = Join-Path $profileDir 'node_modules\dsh-nx\dsh-nx'
$backup  = Join-Path $profileDir 'node_modules\dsh-nx\dsh-nx.profiles-backup'

if (-not (Test-Path -LiteralPath $profileDir)) { throw "profile 不存在: $profileDir" }
if (-not (Test-Path -LiteralPath (Join-Path $target 'client.js'))) {
    throw "目标目录不像 dsh-nx 包（缺少 client.js）: $target"
}

# 已经是软链就只报告
$item = Get-Item -LiteralPath $link -ErrorAction SilentlyContinue
if ($item -and $item.LinkType) {
    "已经是 $($item.LinkType) -> $($item.Target)"
    exit 0
}

if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }

if (Test-Path -LiteralPath $link) {
    try {
        Rename-Item -LiteralPath $link -NewName 'dsh-nx.profiles-backup'
        "已备份原版 -> dsh-nx.profiles-backup"
    }
    catch {
        throw "重命名失败（DSH 还在运行？先完全退出 DSH）: $($_.Exception.Message)"
    }
}

New-Item -ItemType Junction -Path $link -Target $target | Out-Null
$ok = Test-Path -LiteralPath (Join-Path $link 'client.js')
"junction 已建立: $link -> $target   (client.js 可见: $ok)"

# 清掉可能残留的 __pycache__，避免 python 版本不一致
$pyc = Join-Path $target 'python\__pycache__'
if (Test-Path -LiteralPath $pyc) { Remove-Item -LiteralPath $pyc -Recurse -Force -ErrorAction SilentlyContinue }

"现在重新启动 DSH 桌面端即可。"
