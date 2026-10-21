param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$Output,
    [string]$Spec, [string]$Pdf, [string]$Receipt, [string]$Python = "python"
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
function Get-Sha256([string]$Path) {
    $stream=[IO.File]::OpenRead($Path); $algorithm=[Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}
$Source = (Resolve-Path -LiteralPath $Source).Path
$Output = [IO.Path]::GetFullPath($Output)
if ($Source -eq $Output -or (Test-Path -LiteralPath $Output)) { throw '원본이나 기존 결과에는 저장하지 않습니다.' }
if ([IO.Path]::GetExtension($Output) -ne '.hwpx') { throw '출력 확장자는 .hwpx여야 합니다.' }
foreach ($destination in @($Pdf,$Receipt) | Where-Object { $_ }) {
    if (Test-Path -LiteralPath $destination) { throw "기존 결과를 보존했습니다: $destination" }
    if ([IO.Path]::GetFullPath($destination) -eq $Source -or [IO.Path]::GetFullPath($destination) -eq $Output) { throw '출력 경로가 겹칩니다.' }
}
$allPaths=@($Source,$Output) + @(@($Pdf,$Receipt) | Where-Object { $_ } | ForEach-Object { [IO.Path]::GetFullPath($_) })
if (@($allPaths | Select-Object -Unique).Count -ne $allPaths.Count) { throw '출력 경로가 서로 겹칩니다.' }
$sourceHash = Get-Sha256 $Source
$plan = $null
if ($Spec) {
    $plan = Get-Content -LiteralPath $Spec -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($plan.source_sha256 -ne $sourceHash) { throw '현재 원본과 채움 명세의 해시가 다릅니다.' }
    if ($plan.body -and -not $plan.body.anchor) { throw '본문 위치가 없습니다.' }
    if ($plan.body -and @($plan.body.blocks).Count -lt 1) { throw '채울 본문이 없습니다.' }
    foreach ($replacement in @($plan.replacements | Where-Object { $null -ne $_ })) {
        if (-not $replacement.find -or $replacement.count -lt 1) { throw '치환 문자열과 양수 발생 수가 필요합니다.' }
    }
    foreach ($block in @($plan.body.blocks | Where-Object { $null -ne $_ })) {
        if ([string]$block.text -match '[\r\n]') { throw '문단마다 별도 block을 만드세요. 문단 나눔을 문자열에 넣지 않습니다.' }
        if ($block.style_id -isnot [int] -and $block.style_id -isnot [long]) { throw 'style_id는 현재 양식에서 읽은 정수여야 합니다.' }
        if ($block.spacing_before_pt) { throw '간격은 기존 본문 스타일의 빈 문단 block으로 명시하세요.' }
        if ($block.image -and ($block.runs -or $block.code -or [string]$block.text)) { throw '그림은 비어 있는 별도 문단으로 넣으세요.' }
        if ($block.code -and $block.runs) { throw '코드 문단은 text로 입력합니다.' }
        if ($block.image) {
            $imagePath=[IO.Path]::GetFullPath([string]$block.image.path)
            if (-not [IO.Path]::IsPathRooted([string]$block.image.path) -or -not (Test-Path -LiteralPath $imagePath)) { throw '그림은 확인한 로컬 파일의 절대 경로여야 합니다.' }
            if ((Get-Sha256 $imagePath) -ne $block.image.sha256) { throw '그림 원본의 해시가 다릅니다.' }
            foreach ($dimension in @($block.image.width_mm,$block.image.height_mm)) {
                $size=[double]$dimension
                if ([double]::IsNaN($size) -or [double]::IsInfinity($size) -or $size -le 0) { throw '그림의 실제 인쇄 폭과 높이가 필요합니다.' }
            }
        }
        foreach ($part in @($block.runs | Where-Object { $null -ne $_ })) {
            if ([string]$part.text -match '[\r\n]' -or [string]$part.script -match '[\r\n]') { throw 'runs에도 문단 나눔을 넣지 않습니다.' }
            if ($part.kind -notin @('text','equation')) { throw '지원하는 본문 조각은 text/equation입니다.' }
            if ($part.kind -eq 'equation' -and (-not $part.script -or $part.base_pt -lt 6 -or $part.base_pt -gt 30)) { throw '수식 스크립트와 6~30pt 크기가 필요합니다.' }
        }
    }
}
function Require($result,[string]$message) { if (-not $result) { throw $message } }
function Find-Exact($app,[string]$needle) {
    Require ($app.HAction.Run('MoveDocBegin')) '문서 시작 이동 실패'
    $find = $app.HParameterSet.HFindReplace
    $app.HAction.GetDefault('RepeatFind',$find.HSet) | Out-Null
    $find.FindString=$needle; $find.Direction=0; $find.IgnoreMessage=1
    $find.MatchCase=1; $find.UseWildCards=0; $find.FindRegExp=0; $find.WholeWordOnly=0
    Require ($app.HAction.Execute('RepeatFind',$find.HSet)) "본문 위치를 찾지 못했습니다: $needle"
}
function Insert-Text($app,[string]$text) {
    if ($text.Length -eq 0) { return }
    $insert=$app.HParameterSet.HInsertText
    $app.HAction.GetDefault('InsertText',$insert.HSet) | Out-Null
    $insert.Text=$text
    Require ($app.HAction.Execute('InsertText',$insert.HSet)) '본문 입력 실패'
}
function Fill-FormMetadata([string]$path,$replacements) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    Add-Type -AssemblyName System.IO.Compression
    $archive=[IO.Compression.ZipFile]::Open($path,[IO.Compression.ZipArchiveMode]::Update)
    try {
        $xmlFiles=@{}; $counts=@{}; $styleIds=@()
        $map=@{}
        foreach ($replacement in @($replacements | Where-Object { $null -ne $_ })) {
            if ($map.ContainsKey([string]$replacement.find)) { throw '중복 치환 문자열입니다.' }
            $map[[string]$replacement.find]=[string]$replacement.replace
        }
        $pattern=(@($map.Keys | Sort-Object Length -Descending | ForEach-Object { [regex]::Escape($_) }) -join '|')
        foreach ($entry in $archive.Entries) {
            if ($entry.FullName -match '^Contents/(section\d+|header)\.xml$') {
                $reader=[IO.StreamReader]::new($entry.Open(),[Text.Encoding]::UTF8)
                try { $xmlFiles[$entry.FullName]=$reader.ReadToEnd() } finally { $reader.Dispose() }
            }
        }
        $header=[xml]$xmlFiles['Contents/header.xml']
        $styleIds=@($header.SelectNodes("//*[local-name()='style']") | ForEach-Object { [int]$_.id })
        foreach ($replacement in @($replacements | Where-Object { $null -ne $_ })) {
            $counts[[string]$replacement.find]=0
            foreach ($xml in $xmlFiles.Values) {
                foreach ($match in [regex]::Matches($xml,'<hp:t(?:\s[^>]*)?>(.*?)</hp:t>','Singleline')) {
                    foreach ($chunk in [regex]::Split($match.Groups[1].Value,'(<[^>]+>)')) {
                        if (-not $chunk.StartsWith('<')) {
                            $plain=[Net.WebUtility]::HtmlDecode($chunk)
                            $counts[[string]$replacement.find]+=[regex]::Matches($plain,[regex]::Escape([string]$replacement.find)).Count
                        }
                    }
                }
            }
            if ($counts[[string]$replacement.find] -ne $replacement.count) { throw "양식 전체의 치환 발생 수 불일치: $($replacement.find). 여러 글자 조각에 걸친 문구는 네이티브 편집으로 처리하세요." }
        }
        foreach ($name in @($xmlFiles.Keys)) {
            $original=$xmlFiles[$name]
            $updated=[regex]::Replace($original,'(<hp:t(?:\s[^>]*)?>)(.*?)(</hp:t>)',{
                param($match)
                $chunks=[regex]::Split($match.Groups[2].Value,'(<[^>]+>)')
                $newChunks=foreach ($chunk in $chunks) {
                    if ($chunk.StartsWith('<')) { $chunk; continue }
                    $plain=[Net.WebUtility]::HtmlDecode($chunk); $changed=$plain
                    if ($pattern) { $changed=[regex]::Replace($plain,$pattern,{ param($m) $map[$m.Value] }) }
                    if ($changed -eq $plain) { $chunk } else { [Security.SecurityElement]::Escape($changed) }
                }
                $match.Groups[1].Value+($newChunks -join '')+$match.Groups[3].Value
            },'Singleline')
            if ($updated -ne $original) {
                $updated=[regex]::Replace($updated,'<hp:linesegarray\b[^>]*/>|<hp:linesegarray\b[^>]*>.*?</hp:linesegarray>','','Singleline')
                $null=[xml]$updated
                $entry=$archive.GetEntry($name)
                $stream=$entry.Open()
                try { $stream.SetLength(0); $bytes=[Text.Encoding]::UTF8.GetBytes($updated); $stream.Write($bytes,0,$bytes.Length) } finally { $stream.Dispose() }
            }
        }
        return ,$styleIds
    } finally { $archive.Dispose() }
}
function Add-CodeStyles([string]$path,$blocks) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive=[IO.Compression.ZipFile]::Open($path,[IO.Compression.ZipArchiveMode]::Update)
    try {
        $entry=$archive.GetEntry('Contents/header.xml');$reader=[IO.StreamReader]::new($entry.Open(),[Text.Encoding]::UTF8)
        try { $header=[xml]$reader.ReadToEnd() } finally { $reader.Dispose() }
        $fontface=$header.SelectSingleNode("//*[local-name()='fontface' and @lang='LATIN']")
        if (-not $fontface) { throw '양식에서 영문 글꼴 목록을 찾지 못했습니다.' }
        $font=$fontface.SelectSingleNode("*[local-name()='font' and @face='Consolas']")
        if (-not $font) {
            $font=$fontface.FirstChild.CloneNode($true)
            $font.id=[string](1+(@($fontface.ChildNodes | ForEach-Object { [int]$_.id }) | Measure-Object -Maximum).Maximum)
            $font.face='Consolas';$font.type='TTF';$font.isEmbedded='0'
            $null=$fontface.AppendChild($font);$fontface.fontCnt=[string]$fontface.ChildNodes.Count
        }
        $styles=$header.SelectSingleNode("//*[local-name()='styles']")
        $chars=$header.SelectSingleNode("//*[local-name()='charProperties']")
        $paras=$header.SelectSingleNode("//*[local-name()='paraProperties']")
        $map=@{}
        foreach ($baseId in @($blocks | Where-Object { $_.code } | ForEach-Object { [int]$_.style_id } | Select-Object -Unique)) {
            $base=$styles.SelectSingleNode("*[local-name()='style' and @id='$baseId']")
            $char=$chars.SelectSingleNode("*[local-name()='charPr' and @id='$($base.charPrIDRef)']").CloneNode($true)
            $para=$paras.SelectSingleNode("*[local-name()='paraPr' and @id='$($base.paraPrIDRef)']").CloneNode($true)
            $char.id=[string](1+(@($chars.ChildNodes | ForEach-Object { [int]$_.id }) | Measure-Object -Maximum).Maximum)
            $para.id=[string](1+(@($paras.ChildNodes | ForEach-Object { [int]$_.id }) | Measure-Object -Maximum).Maximum)
            $char.SelectSingleNode("*[local-name()='fontRef']").SetAttribute('latin',[string]$font.id)
            $para.SelectSingleNode("*[local-name()='align']").SetAttribute('horizontal','LEFT')
            $null=$chars.AppendChild($char);$null=$paras.AppendChild($para)
            $style=$base.CloneNode($true)
            $style.id=[string](1+(@($styles.ChildNodes | ForEach-Object { [int]$_.id }) | Measure-Object -Maximum).Maximum)
            $style.name='시험지 코드 '+$baseId;$style.engName='ExamCode'+$baseId
            $style.charPrIDRef=$char.id;$style.paraPrIDRef=$para.id;$style.nextStyleIDRef=$style.id
            $null=$styles.AppendChild($style);$map[[int]$baseId]=[int]$style.id
        }
        foreach ($collection in @($styles,$chars,$paras)) { $collection.itemCnt=[string]$collection.ChildNodes.Count }
        $stream=$entry.Open();try { $bytes=[Text.Encoding]::UTF8.GetBytes($header.OuterXml);$stream.SetLength(0);$stream.Write($bytes,0,$bytes.Length) } finally { $stream.Dispose() }
        return $map
    } finally { $archive.Dispose() }
}
$app=$null; $saved=$false; $flatCopy=$null; $boxedCopy=$null; $boxReceipt=$null
$workingCopy=Join-Path (Split-Path -Parent $Output) ('.exam-working-'+[guid]::NewGuid().ToString('N')+[IO.Path]::GetExtension($Source))
Copy-Item -LiteralPath $Source -Destination $workingCopy
try {
    if ($plan -and [IO.Path]::GetExtension($Source) -ne '.hwpx') { throw '내용 입력 전 HWP를 HWPX 사본으로 변환하고 해당 사본을 검사하세요.' }
    if ($plan) {
        $styleIds=Fill-FormMetadata $workingCopy @($plan.replacements)
        foreach ($block in @($plan.body.blocks | Where-Object { $null -ne $_ })) {
            if ($null -eq $block.style_id -or [int]$block.style_id -notin $styleIds) { throw '본문 문단마다 현재 양식에 존재하는 style_id가 필요합니다.' }
        }
        $codeStyles=@{}
        if (@($plan.body.blocks | Where-Object { $_.code }).Count) { $codeStyles=Add-CodeStyles $workingCopy @($plan.body.blocks) }
    }
    $app=New-Object -ComObject HWPFrame.HwpObject
    $app.XHwpWindows.Item(0).Visible=$false
    $modules=Get-ItemProperty -Path 'HKCU:\Software\HNC\HwpAutomation\Modules' -ErrorAction SilentlyContinue
    $moduleRegistered=$false
    if ($modules) {
        foreach ($property in $modules.PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' -and $_.Value -is [string] }) {
            if (Test-Path -LiteralPath $property.Value) {
                if ($app.RegisterModule('FilePathCheckDLL',$property.Name)) { $moduleRegistered=$true; break }
            }
        }
    }
    if (-not $moduleRegistered) {
        Write-Warning '한글의 파일 접근 확인 창이 나타나면 사용자가 허용해야 합니다. 별도 보안 모듈은 설치하지 않습니다.'
        $app.XHwpWindows.Item(0).Visible=$true
    }
    Write-Verbose '한글 사본 열기 시작'
    Require ($app.Open($workingCopy,'','versionwarning:false')) '한글 사본 열기 실패'
    Write-Verbose '한글 사본 열기 완료'
    if ($plan) {
        if ($plan.body) {
            $text=[string]$app.GetTextFile('TEXT','')
            if ([regex]::Matches($text,[regex]::Escape([string]$plan.body.anchor)).Count -ne 1) { throw '본문 위치는 문서에서 하나여야 합니다.' }
            Find-Exact $app ([string]$plan.body.anchor)
            Require ($app.HAction.Run('Delete')) '본문 표시자 제거 실패'
            $first=$true
            $blockIndex=0
            foreach ($block in @($plan.body.blocks)) {
                Write-Verbose ('본문 block '+$blockIndex);$blockIndex++
                if (-not $first) { Require ($app.HAction.Run('BreakPara')) '문단 나누기 실패' }
                $first=$false
                if ($block.break_before -eq 'column') { Require ($app.HAction.Run('BreakColumn')) '단 나누기 실패' }
                elseif ($block.break_before -eq 'page') { Require ($app.HAction.Run('BreakPage')) '쪽 나누기 실패' }
                elseif ($block.break_before) { throw '지원하지 않는 나누기입니다.' }
                if ($null -ne $block.style_id) {
                    $style=$app.HParameterSet.HStyle
                    $app.HAction.GetDefault('StyleEx',$style.HSet) | Out-Null
                    $style.Apply=if ($block.code) { $codeStyles[[int]$block.style_id] } else { [int]$block.style_id }
                    Require ($app.HAction.Execute('StyleEx',$style.HSet)) '기존 스타일 적용 실패'
                }
                if ($block.image) {
                    $picture=$app.InsertPicture([string]$block.image.path,$true,1,$false,$false,0,[int]$block.image.width_mm,[int]$block.image.height_mm)
                    if ($null -eq $picture) { throw '그림 삽입 실패' }
                    $shape=$picture.Properties
                    $shape.SetItem('TreatAsChar',1)
                    $picture.Properties=$shape
                    Require ($app.HAction.Run('Cancel')) '그림 선택 해제 실패'
                    Require ($app.HAction.Run('MoveLineEnd')) '그림 문단 끝 이동 실패'
                } elseif ($block.runs) {
                    foreach ($part in @($block.runs)) {
                        if ($part.kind -eq 'text') { Insert-Text $app ([string]$part.text) }
                        else {
                            $equation=$app.HParameterSet.HEqEdit
                            $app.HAction.GetDefault('EquationCreate',$equation.HSet) | Out-Null
                            $equation.String=[string]$part.script; $equation.BaseUnit=[int]([double]$part.base_pt*100)
                            $equation.EqFontName='HYhwpEQ'; $equation.TreatAsChar=1
                            Require ($app.HAction.Execute('EquationCreate',$equation.HSet)) '편집 가능한 수식 입력 실패'
                            Require ($app.HAction.Run('Cancel')) '수식 선택 해제 실패'
                            Require ($app.HAction.Run('MoveLineEnd')) '수식 뒤 이동 실패'
                        }
                    }
                } else { Insert-Text $app ([string]$block.text) }
            }
        }
    }
    if ($plan -and $plan.body.boxes -and @($plan.body.boxes.PSObject.Properties).Count -gt 0) {
        $flatCopy=Join-Path (Split-Path -Parent $Output) ('.exam-flat-'+[guid]::NewGuid().ToString('N')+'.hwpx')
        $boxedCopy=Join-Path (Split-Path -Parent $Output) ('.exam-boxed-'+[guid]::NewGuid().ToString('N')+'.hwpx')
        Require ($app.SaveAs($flatCopy,'HWPX','')) '상자 입력 전 한글 저장 실패'
        $app.Clear(1) | Out-Null
        $wrapper=Join-Path $PSScriptRoot 'wrap_boxes.py'
        $wrapJson=& $Python -B $wrapper --source $flatCopy --output $boxedCopy --template $Source --plan $Spec
        if ($LASTEXITCODE -ne 0) { throw '원본 상자 복제 실패' }
        $boxReceipt=$wrapJson | ConvertFrom-Json
        Require ($app.Open($boxedCopy,'HWPX','versionwarning:false')) '상자 복제 후 한글 열기 실패'
    }
    # PageCount forces complete pagination. Save the calculated geometry, not
    # stale table caches left by replacement or a lazily opened document.
    $pages=[int]$app.PageCount
    Require ($app.SaveAs($Output,'HWPX','')) 'HWPX 저장 실패'
    $saved=$true
    if ($Pdf) {
        $Pdf=[IO.Path]::GetFullPath($Pdf)
        Require ($app.SaveAs($Pdf,'PDF','')) '같은 한글 문서의 PDF 출력 실패'
    }
    if ((Get-Sha256 $Source) -ne $sourceHash) { throw '원본 해시가 달라졌습니다.' }
    $result=[ordered]@{status='NATIVE_EXPORTED';source_sha256=$sourceHash;output_sha256=(Get-Sha256 $Output);pages=$pages;visual_review='PENDING';content_review='PENDING';template_review='PENDING'}
    if ($Pdf) { $result.pdf_sha256=Get-Sha256 $Pdf }
    if ($boxReceipt) { $result.boxes=$boxReceipt }
    $json=$result | ConvertTo-Json -Depth 5
    if ($Receipt) { [IO.File]::WriteAllText([IO.Path]::GetFullPath($Receipt),$json,[Text.UTF8Encoding]::new($false)) }
    $json
} finally {
    if ($app) {
        try { $app.Clear(1) | Out-Null; $app.Quit() | Out-Null } finally { [Runtime.InteropServices.Marshal]::FinalReleaseComObject($app) | Out-Null }
    }
    foreach ($temporary in @($workingCopy,$flatCopy,$boxedCopy) | Where-Object { $_ }) {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}
