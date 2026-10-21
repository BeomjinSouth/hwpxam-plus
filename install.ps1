param(
    [ValidateSet('both','codex','claude')][string]$Target = 'both',
    [string]$SkillsRoot,
    [string]$UserHome = [Environment]::GetFolderPath('UserProfile'),
    [switch]$Upgrade
)
$savedOutputEncoding = $null
try {
    $savedOutputEncoding = [Console]::OutputEncoding
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
} catch {}
$exitCode = 0
$ErrorActionPreference = 'Stop'
function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Assert-NoLink([string]$Path) {
    if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "연결된 폴더와 파일은 설치하지 않습니다: $Path"
    }
}
function Get-SkillFiles([string]$Root) {
    Assert-NoLink $Root
    foreach ($node in @(Get-ChildItem -LiteralPath $Root -Force)) {
        Assert-NoLink $node.FullName
        if ($node.PSIsContainer) {
            # Check excluded directories for links too.
            $children = @(Get-SkillFiles $node.FullName)
            if ($node.Name -ne '__pycache__') { $children }
        } elseif ($node.Name -notlike '*.pyc') { $node }
    }
}
function Resolve-PackageFile([string]$Relative) {
    if (-not $Relative -or [IO.Path]::IsPathRooted($Relative) -or
        $Relative -match '(^|[\\/])\.\.([\\/]|$)' -or $Relative.Contains(':')) {
        throw "잘못된 상대 경로입니다: $Relative"
    }
    $full = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $Relative))
    if (-not $full.StartsWith($packagePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "묶음 밖의 경로입니다: $Relative"
    }
    $cursor = $PSScriptRoot
    Assert-NoLink $cursor
    foreach ($part in $Relative.Split([char[]]'\/')) {
        if (-not $part -or $part -eq '.') { continue }
        $cursor = Join-Path $cursor $part
        if (-not (Test-Path -LiteralPath $cursor)) { throw "묶음에 없는 파일입니다: $Relative" }
        Assert-NoLink $cursor
    }
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "묶음에 없는 파일입니다: $Relative" }
    return $full
}
function Test-Installed([string]$Path, [object[]]$Entries, [string]$Skill) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    $actual = @(Get-SkillFiles $Path)
    if ($actual.Count -ne $Entries.Count) { return $false }
    $names = @{}
    foreach ($file in $actual) { $names[$file.FullName.Substring($Path.Length + 1).Replace('\','/')] = $file.FullName }
    foreach ($entry in $Entries) {
        $relative = $entry.path.Substring($Skill.Length + 1)
        if (-not $names.ContainsKey($relative)) { return $false }
        if ((Get-Sha256 $names[$relative]) -ne $entry.sha256) { return $false }
    }
    return $true
}
function Get-SkillName([string]$Path, [string]$Skill) {
    $text = [IO.File]::ReadAllText((Join-Path $Path 'SKILL.md'))
    $front = [regex]::Match($text, '\A\uFEFF?---\r?\n(.*?)\r?\n---(?:\r?\n|$)', 'Singleline')
    $name = [regex]::Match($front.Groups[1].Value, '(?m)^name:\s*([^\r\n]+)')
    $value = $name.Groups[1].Value.Trim().Trim([char[]]'"''')
    if (-not $front.Success -or -not $name.Success -or $value -cne $Skill) {
        throw "설치한 파일 검증에 실패했습니다: $Path"
    }
    return $value
}
function Install-Staged([string]$Source, [string]$Destination) {
    $parent = Split-Path -Parent $Destination
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $stage = Join-Path $parent ('.hwpxam-install-' + [guid]::NewGuid().ToString('N'))
    try {
        [IO.Directory]::CreateDirectory($stage) | Out-Null
        foreach ($file in @(Get-SkillFiles $Source)) {
            $copy = Join-Path $stage $file.FullName.Substring($Source.Length + 1)
            [IO.Directory]::CreateDirectory((Split-Path -Parent $copy)) | Out-Null
            [IO.File]::Copy($file.FullName, $copy)
        }
        if (Test-Path -LiteralPath $Destination) { throw "설치 중 같은 이름의 폴더가 생겼습니다: $Destination" }
        [IO.Directory]::Move($stage, $Destination)
    } finally {
        if (Test-Path -LiteralPath $stage) {
            $resolved = [IO.Path]::GetFullPath($stage)
            if ((Split-Path -Parent $resolved) -ne $parent -or (Split-Path -Leaf $resolved) -notlike '.hwpxam-install-*') {
                throw "임시 폴더 정리 범위가 맞지 않습니다: $stage"
            }
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}
try {
    try {
        $packagePrefix = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar
        Assert-NoLink (Join-Path $PSScriptRoot 'MANIFEST.json')
        $manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'MANIFEST.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $entries = @($manifest.files)
        if (-not $entries.Count) { throw '파일 목록이 비어 있습니다.' }
        $seen = @{}
        foreach ($entry in $entries) {
            $relative = [string]$entry.path
            $full = Resolve-PackageFile $relative
            if ($seen.ContainsKey($full)) { throw "잘못된 상대 경로입니다: $relative" }
            $seen[$full] = $true
            if ((Get-Sha256 $full) -ne $entry.sha256) {
                throw "파일 검증 실패: $relative. GitHub에서 받았다면 줄바꿈 자동 변환으로 내용이 바뀌었을 수 있습니다. ZIP으로 다시 받거나 git -c core.autocrlf=false clone 으로 다시 받으세요."
            }
        }
        $skillEntries = @{}
        foreach ($skill in @($manifest.skills)) {
            if (-not $skill -or $skill -match '[\\/:]' -or $skill -eq '.' -or $skill -eq '..') { throw "잘못된 상대 경로입니다: $skill" }
            $source = Join-Path $PSScriptRoot $skill
            if (-not (Test-Path -LiteralPath (Join-Path $source 'SKILL.md') -PathType Leaf)) { throw "SKILL.md가 없습니다: $skill" }
            $actual = @(Get-SkillFiles $source | ForEach-Object { $_.FullName.Substring($packagePrefix.Length).Replace('\','/') })
            $listed = @($entries | Where-Object { $_.path.StartsWith($skill + '/', [StringComparison]::Ordinal) })
            if ($actual.Count -ne $listed.Count -or @($actual | Where-Object { $_ -cnotin @($listed.path) }).Count) {
                throw "스킬 폴더에 기록에 없는 파일이나 빠진 파일이 있습니다: $skill"
            }
            $skillEntries[$skill] = $listed
            Get-SkillName $source $skill | Out-Null
        }
        if (-not $skillEntries.Count) { throw '파일 목록이 비어 있습니다.' }
        $roots = @()
        if ($SkillsRoot) { $roots += @{ tool = 'custom'; root = $SkillsRoot } }
        else {
            if ($Target -in @('both','codex')) { $roots += @{ tool = 'codex'; root = (Join-Path $UserHome '.agents\skills') } }
            if ($Target -in @('both','claude')) {
                $claudeRoot = Join-Path $UserHome '.claude'
                if (-not [string]::IsNullOrWhiteSpace($env:CLAUDE_CONFIG_DIR)) { $claudeRoot = $env:CLAUDE_CONFIG_DIR }
                $roots += @{ tool = 'claude'; root = (Join-Path $claudeRoot 'skills') }
            }
        }
        $results = @()
        $exitCode = 0
        foreach ($skill in @($manifest.skills)) {
            foreach ($root in $roots) {
                $path = [IO.Path]::GetFullPath((Join-Path $root.root $skill)).TrimEnd('\','/')
                $source = Join-Path $PSScriptRoot $skill
                $listed = @($skillEntries[$skill])
                $result = [ordered]@{ tool = $root.tool; skill = $skill; path = $path; status = ''; files = $listed.Count }
                if (-not (Test-Path -LiteralPath $path)) {
                    Install-Staged $source $path
                    $result.status = 'INSTALLED'
                } elseif (Test-Installed $path $listed $skill) {
                    $result.status = 'ALREADY_INSTALLED'
                } elseif (-not $Upgrade) {
                    $result.status = 'PRESERVED_DIFFERENT'
                    $exitCode = 3
                } else {
                    $backupRoot = [IO.Path]::GetFullPath((Join-Path $UserHome '.teacher-exam-hwpx\backups'))
                    [IO.Directory]::CreateDirectory($backupRoot) | Out-Null
                    $base = Join-Path $backupRoot ((Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $root.tool + '-' + $skill)
                    $backup = $base
                    $suffix = 2
                    while (Test-Path -LiteralPath $backup) { $backup = $base + '-' + $suffix; $suffix++ }
                    [IO.Directory]::Move($path, $backup)
                    try { Install-Staged $source $path }
                    catch {
                        if (-not (Test-Path -LiteralPath $path)) { [IO.Directory]::Move($backup, $path) }
                        throw
                    }
                    $result.status = 'UPGRADED'
                    $result.backup = $backup
                }
                if ($result.status -ne 'PRESERVED_DIFFERENT') {
                    if (-not (Test-Installed $path $listed $skill)) { throw "설치한 파일 검증에 실패했습니다: $path" }
                    $result.verified = $true
                    $result.skill_md_name = Get-SkillName $path $skill
                }
                $results += [PSCustomObject]$result
            }
        }
        ConvertTo-Json -InputObject ([ordered]@{ package = $manifest.package; version = $manifest.version; results = @($results); closing_line = $manifest.closing_line }) -Depth 5
        if ($exitCode -eq 0) { Write-Output $manifest.closing_line }
    } catch {
        ConvertTo-Json -InputObject @{ status = 'FAIL'; error = $_.Exception.Message } -Depth 5
        $exitCode = 1
    }
} finally {
    if ($null -ne $savedOutputEncoding) {
        try { [Console]::OutputEncoding = $savedOutputEncoding } catch {}
    }
}
exit $exitCode
