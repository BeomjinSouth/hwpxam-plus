param([string]$TestRoot = (Join-Path ([IO.Path]::GetTempPath()) 'hwpxam-test'))
$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
$manifest = Get-Content -LiteralPath (Join-Path $repository 'MANIFEST.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'package-config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$runRoot = Join-Path ([IO.Path]::GetFullPath($TestRoot)) ('실행 ' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($runRoot) | Out-Null
$rows = @()
$skills = @($manifest.skills)
$firstSkill = $skills[0]
$savedClaude = $env:CLAUDE_CONFIG_DIR
function Assert([bool]$Condition, [string]$Detail) {
    if (-not $Condition) { throw "검사 실패: $Detail" }
}
function New-Package([string]$Name) {
    $package = Join-Path $runRoot $Name
    [IO.Directory]::CreateDirectory($package) | Out-Null
    foreach ($relative in @('MANIFEST.json') + @($manifest.files | ForEach-Object path)) {
        $destination = Join-Path $package $relative
        [IO.Directory]::CreateDirectory((Split-Path -Parent $destination)) | Out-Null
        Copy-Item -LiteralPath (Join-Path $repository $relative) -Destination $destination
    }
    return $package
}
function Read-InstallerOutput([string]$Text, [int]$Code) {
    $first = $Text.IndexOf('{')
    $last = $Text.LastIndexOf('}')
    Assert ($first -ge 0 -and $last -ge $first) 'JSON output'
    $json = $Text.Substring($first, $last - $first + 1)
    $trailer = $Text.Substring($last + 1)
    if ($Code -eq 0) {
        Assert ($trailer.Trim() -ceq $manifest.closing_line) 'success trailer'
        Assert ($trailer.Trim().Contains('https://github.com/BeomjinSouth/')) 'trailer repository link'
    } else {
        Assert ($trailer.Trim() -ceq '') 'empty failure/preserved trailer'
    }
    return @{ code = $Code; data = ($json | ConvertFrom-Json); raw = $Text; trailer = $trailer }
}
function Invoke-Installer([string]$Package, [string]$TestHome, [string[]]$Extra = @()) {
    $savedOutputEncoding = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        $raw = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Package 'install.ps1') -UserHome $TestHome @Extra
        $code = $LASTEXITCODE
    } finally { [Console]::OutputEncoding = $savedOutputEncoding }
    $text = $raw -join "`n"
    return (Read-InstallerOutput $text $code)
}
function Assert-Success($Invocation, [int]$Count) {
    Assert ($Invocation.code -eq 0) $Invocation.raw
    Assert ($Invocation.data.results -is [array]) 'results array'
    Assert ($Invocation.data.results.Count -eq ($Count * $skills.Count)) 'results count'
    Assert ($Invocation.data.package -ceq $manifest.package -and $Invocation.data.version -ceq $manifest.version) 'package/version'
    Assert ($Invocation.data.closing_line -ceq $config.closing_line) 'closing_line'
    foreach ($result in $Invocation.data.results) {
        $prefix = $result.skill + '/'
        $entries = @($manifest.files | Where-Object { $_.path.StartsWith($prefix, [StringComparison]::Ordinal) })
        Assert ($result.skill -cin $skills -and $result.files -eq $entries.Count -and $result.verified -eq $true -and $result.skill_md_name -ceq $result.skill) 'verification fields'
        foreach ($entry in $entries) {
            $path = Join-Path $result.path $entry.path.Substring($prefix.Length)
            Assert ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -eq $entry.sha256) $path
        }
        $actual = @(Get-ChildItem -LiteralPath $result.path -Recurse -Force -File | Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Name -notlike '*.pyc' })
        Assert ($actual.Count -eq $entries.Count) 'installed file set'
    }
}
function Assert-Targets($Invocation, [string]$TestHome, [string[]]$Tools) {
    foreach ($skill in $skills) {
        foreach ($tool in $Tools) {
            $parent = if ($tool -eq 'codex') { '.agents\skills' } else { '.claude\skills' }
            $expected = Join-Path (Join-Path $TestHome $parent) $skill
            $matches = @($Invocation.data.results | Where-Object { $_.skill -ceq $skill -and $_.tool -eq $tool -and $_.path -eq $expected })
            Assert ($matches.Count -eq 1 -and (Test-Path -LiteralPath $expected)) 'target path'
        }
    }
}
function Snapshot([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    return (@(Get-ChildItem -LiteralPath $Path -File -Recurse -Force | Sort-Object FullName | ForEach-Object {
        $_.FullName.Substring($Path.Length) + ':' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }) -join "`n")
}
function Run-Case([int]$Number, [scriptblock]$Body) {
    try { & $Body; $script:rows += [PSCustomObject]@{ case = $Number; status = 'PASS'; detail = '' } }
    catch { $script:rows += [PSCustomObject]@{ case = $Number; status = 'FAIL'; detail = $_.Exception.Message } }
}
try {
    $env:CLAUDE_CONFIG_DIR = $null
    $testHome = Join-Path $runRoot '교사 홈'
    $codexPath = Join-Path $testHome ('.agents\skills\' + $firstSkill)
    $claudePath = Join-Path $testHome ('.claude\skills\' + $firstSkill)
    $changedFile = Join-Path $claudePath 'SKILL.md'
    Run-Case 1 {
        $p = New-Package '사례 01'
        $r = Invoke-Installer $p $testHome
        Assert-Success $r 2
        Assert (@($r.data.results | Where-Object status -ne 'INSTALLED').Count -eq 0) 'INSTALLED'
        Assert (@($r.data.results.tool | Sort-Object -Unique) -join ',' -eq 'claude,codex') 'both tools'
        Assert-Targets $r $testHome @('codex', 'claude')
    }
    Run-Case 2 {
        $p = New-Package '사례 02'
        $r = Invoke-Installer $p $testHome
        Assert-Success $r 2
        Assert (@($r.data.results | Where-Object status -ne 'ALREADY_INSTALLED').Count -eq 0) 'ALREADY_INSTALLED'
        Assert-Targets $r $testHome @('codex', 'claude')
    }
    Run-Case 3 {
        $p = New-Package '사례 03'
        [IO.File]::AppendAllText($changedFile, "`r`nTEST CHANGE`r`n")
        $script:changedHash = (Get-FileHash -LiteralPath $changedFile).Hash
        $r = Invoke-Installer $p $testHome
        Assert ($r.code -eq 3) $r.raw
        Assert (($r.data.results | Where-Object { $_.tool -eq 'claude' -and $_.skill -eq $firstSkill }).status -eq 'PRESERVED_DIFFERENT') 'PRESERVED_DIFFERENT'
        Assert (($r.data.results | Where-Object { $_.tool -eq 'codex' -and $_.skill -eq $firstSkill }).status -eq 'ALREADY_INSTALLED') 'codex unchanged'
        Assert ((Get-FileHash -LiteralPath $changedFile).Hash -eq $changedHash) 'preserved content'
    }
    Run-Case 4 {
        $p = New-Package '사례 04'
        $r = Invoke-Installer $p $testHome @('-Upgrade')
        Assert-Success $r 2
        $result = $r.data.results | Where-Object { $_.tool -eq 'claude' -and $_.skill -eq $firstSkill }
        Assert ($result.status -eq 'UPGRADED') 'UPGRADED'
        Assert ((Split-Path -Parent $result.backup) -eq (Join-Path $testHome '.teacher-exam-hwpx\backups')) 'backup root'
        Assert ((Get-FileHash -LiteralPath (Join-Path $result.backup 'SKILL.md')).Hash -eq $changedHash) 'backup content'
        foreach ($parent in @((Split-Path -Parent $codexPath), (Split-Path -Parent $claudePath))) {
            Assert (@(Get-ChildItem -LiteralPath $parent -Force).Count -eq $skills.Count) 'no backup/stage under skills'
        }
    }
    Run-Case 5 {
        $p = New-Package '사례 05'
        [IO.File]::AppendAllText((Join-Path $p 'README.md'), 'TAMPER')
        $before = Snapshot $testHome
        $r = Invoke-Installer $p $testHome
        Assert ($r.code -eq 1 -and $r.data.status -eq 'FAIL') $r.raw
        Assert ((Snapshot $testHome) -ceq $before) 'unchanged targets'
    }
    Run-Case 6 {
        $p = New-Package '사례 06'
        [IO.File]::WriteAllText((Join-Path $p 'teacher-exam-hwpx\extra.txt'), 'extra')
        $freshHome = Join-Path $runRoot '사례 06 교사 홈'
        $r = Invoke-Installer $p $freshHome
        Assert ($r.code -eq 1 -and $r.data.status -eq 'FAIL') $r.raw
        Assert (-not (Test-Path -LiteralPath $freshHome)) 'no installation'
    }
    Run-Case 7 {
        $p = New-Package '사례 07'
        foreach ($relative in @('.git\HEAD','.git\objects\sample','.gitattributes','tools\sample.txt')) {
            $path = Join-Path $p $relative
            [IO.Directory]::CreateDirectory((Split-Path -Parent $path)) | Out-Null
            [IO.File]::WriteAllText($path, 'ignored')
        }
        $r = Invoke-Installer $p (Join-Path $runRoot '사례 07 교사 홈')
        Assert-Success $r 2
        Assert (@($r.data.results | Where-Object status -ne 'INSTALLED').Count -eq 0) 'INSTALLED'
    }
    Run-Case 8 {
        $p = New-Package '사례 08'
        $freshHome = Join-Path $runRoot '사례 08 교사 홈'
        $r = Invoke-Installer $p $freshHome @('-Target','codex')
        Assert-Success $r 1
        Assert-Targets $r $freshHome @('codex')
        Assert ($r.data.results[0].tool -eq 'codex' -and $r.data.results[0].status -eq 'INSTALLED') 'codex only'
        Assert (-not (Test-Path -LiteralPath (Join-Path $freshHome '.claude'))) 'no claude'
    }
    Run-Case 9 {
        $p = New-Package '사례 09'
        $file = Join-Path $p ($firstSkill + '\SKILL.md')
        $bytes = [IO.File]::ReadAllBytes($file)
        $text = [Text.Encoding]::UTF8.GetString($bytes)
        Assert ($text.Contains("`r`n")) 'CRLF fixture'
        [IO.File]::WriteAllBytes($file, [Text.Encoding]::UTF8.GetBytes($text.Replace("`r`n","`n")))
        $freshHome = Join-Path $runRoot '사례 09 교사 홈'
        $r = Invoke-Installer $p $freshHome
        Assert ($r.code -eq 1 -and $r.data.status -eq 'FAIL' -and $r.data.error.Contains('git -c core.autocrlf=false clone')) $r.raw
        Assert (-not (Test-Path -LiteralPath $freshHome)) 'no installation'
    }
    Run-Case 10 {
        $p = New-Package '사례 10'
        $env:CLAUDE_CONFIG_DIR = Join-Path $runRoot '별도 Claude 설정'
        $freshHome = Join-Path $runRoot '사례 10 교사 홈'
        try {
            $r = Invoke-Installer $p $freshHome
            Assert-Success $r 2
            Assert (($r.data.results | Where-Object { $_.tool -eq 'claude' -and $_.skill -eq $firstSkill }).path -eq (Join-Path $env:CLAUDE_CONFIG_DIR ('skills\' + $firstSkill))) 'CLAUDE_CONFIG_DIR'
            Assert (-not (Test-Path -LiteralPath (Join-Path $freshHome '.claude'))) 'no default claude'
        } finally { $env:CLAUDE_CONFIG_DIR = $null }
    }
    Run-Case 11 {
        $p = New-Package '사례 11'
        $cache = Join-Path $codexPath 'scripts\__pycache__'
        [IO.Directory]::CreateDirectory($cache) | Out-Null
        [IO.File]::WriteAllText((Join-Path $cache 'x.pyc'), 'cache')
        $r = Invoke-Installer $p $testHome
        Assert-Success $r 2
        Assert (@($r.data.results | Where-Object status -ne 'ALREADY_INSTALLED').Count -eq 0) 'ignore cache'
    }
    Run-Case 12 {
        $p = New-Package '사례 12'
        $custom = Join-Path $runRoot '사용자 스킬 폴더'
        $freshHome = Join-Path $runRoot '사례 12 교사 홈'
        $r = Invoke-Installer $p $freshHome @('-SkillsRoot',$custom,'-Target','both')
        Assert-Success $r 1
        Assert ($r.data.results[0].tool -eq 'custom' -and $r.data.results[0].status -eq 'INSTALLED') 'custom result'
        Assert ($r.data.results[0].path -eq (Join-Path $custom $firstSkill)) 'custom path'
        Assert (-not (Test-Path -LiteralPath $freshHome)) 'custom only'
    }
    Run-Case 13 {
        $p = New-Package '사례 13'
        $freshHome = Join-Path $runRoot '사례 13 교사 홈'
        $outputFile = Join-Path $p 'out.bin'
        $installer = Join-Path $p 'install.ps1'
        $command = 'chcp 949 >nul & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" -UserHome "{1}" > "{2}" 2>&1' -f $installer, $freshHome, $outputFile
        $savedOutputEncoding = [Console]::OutputEncoding
        try {
            & cmd.exe /d /c $command
            $code = $LASTEXITCODE
        } finally { [Console]::OutputEncoding = $savedOutputEncoding }
        $bytes = [IO.File]::ReadAllBytes($outputFile)
        $text = [Text.UTF8Encoding]::new($false, $true).GetString($bytes)
        $r = Read-InstallerOutput $text $code
        Assert-Success $r 2
        Assert ($r.data.closing_line -ceq $manifest.closing_line) 'closing_line exact'
        Assert (@($r.data.results | Where-Object status -ne 'INSTALLED').Count -eq 0) 'INSTALLED'
    }
} finally { $env:CLAUDE_CONFIG_DIR = $savedClaude }
$rows | Format-Table -AutoSize -Wrap
$failed = @($rows | Where-Object status -eq 'FAIL').Count
Write-Output ('PASS: {0}; FAIL: {1}; TestRoot: {2}' -f ($rows.Count - $failed), $failed, $runRoot)
if ($failed -gt 0) { exit 1 }
exit 0
