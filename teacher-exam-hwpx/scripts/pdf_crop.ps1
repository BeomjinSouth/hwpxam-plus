param(
 [Parameter(Mandatory=$true)][string]$Source,
 [Parameter(Mandatory=$true)][int]$Page,
 [Parameter(Mandatory=$true)][string]$Output,
 [string]$Region='0,0,1,1',
 [ValidateRange(72,600)][int]$Dpi=300,
 [string]$Receipt
)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$cropRegion=@($Region.Split(',') | ForEach-Object { [double]::Parse($_,[Globalization.CultureInfo]::InvariantCulture) })
function Hash([string]$Path) {
 $s=[IO.File]::OpenRead($Path);$a=[Security.Cryptography.SHA256]::Create()
 try { ([BitConverter]::ToString($a.ComputeHash($s))).Replace('-','').ToLowerInvariant() }
 finally { $a.Dispose();$s.Dispose() }
}
$Source=(Resolve-Path -LiteralPath $Source).Path
$Output=[IO.Path]::GetFullPath($Output)
if ([IO.Path]::GetExtension($Output) -ne '.png' -or $Source -eq $Output -or (Test-Path -LiteralPath $Output)) { throw '새 PNG 경로가 필요합니다. 원본과 기존 파일은 보존합니다.' }
if ($Receipt -and ((Test-Path -LiteralPath $Receipt) -or [IO.Path]::GetFullPath($Receipt) -in @($Source,$Output))) { throw '기록 경로가 겹치거나 이미 있습니다.' }
if ($Page -lt 1 -or $cropRegion.Count -ne 4 -or @($cropRegion | Where-Object { [double]::IsNaN($_) -or [double]::IsInfinity($_) -or $_ -lt 0 -or $_ -gt 1 }).Count -or $cropRegion[0] -ge $cropRegion[2] -or $cropRegion[1] -ge $cropRegion[3]) { throw '쪽수는 1부터이며 Region은 왼쪽·위·오른쪽·아래의 0~1 좌표여야 합니다.' }
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -AssemblyName System.Drawing
$null=[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null=[Windows.Data.Pdf.PdfDocument,Windows.Data.Pdf,ContentType=WindowsRuntime]
$null=[Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$null=[Windows.Data.Pdf.PdfPageRenderOptions,Windows.Data.Pdf,ContentType=WindowsRuntime]
function AwaitResult($op,[type]$type) {
 $m=@([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 })[0]
 $task=$m.MakeGenericMethod($type).Invoke($null,@($op))
 $task.GetAwaiter().GetResult()
}
function AwaitAction($op) {
 $m=@([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and -not $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]
 $task=$m.Invoke($null,@($op));$null=$task.GetAwaiter().GetResult()
}
$pdfPage=$null;$random=$null;$stream=$null;$bitmap=$null;$crop=$null
$sourceHash=Hash $Source
try {
 $file=AwaitResult ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Source)) ([Windows.Storage.StorageFile])
 $doc=AwaitResult ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
 if ($Page -gt $doc.PageCount) { throw 'PDF 쪽수를 벗어났습니다.' }
 $pdfPage=$doc.GetPage([uint32]($Page-1))
 $options=New-Object Windows.Data.Pdf.PdfPageRenderOptions
 $options.DestinationWidth=[uint32][Math]::Round($pdfPage.Size.Width*$Dpi/96)
 $options.DestinationHeight=[uint32][Math]::Round($pdfPage.Size.Height*$Dpi/96)
 $random=New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
 AwaitAction ($pdfPage.RenderToStreamAsync($random,$options))
 $random.Seek(0)
 $stream=[System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($random)
 $bitmap=[Drawing.Bitmap]::new($stream)
 $left=[int][Math]::Floor($cropRegion[0]*$bitmap.Width);$top=[int][Math]::Floor($cropRegion[1]*$bitmap.Height)
 $right=[int][Math]::Ceiling($cropRegion[2]*$bitmap.Width);$bottom=[int][Math]::Ceiling($cropRegion[3]*$bitmap.Height)
 $rect=[Drawing.Rectangle]::new($left,$top,$right-$left,$bottom-$top)
 $crop=$bitmap.Clone($rect,[Drawing.Imaging.PixelFormat]::Format32bppArgb)
 $crop.Save($Output,[Drawing.Imaging.ImageFormat]::Png)
 if ((Hash $Source) -ne $sourceHash) { throw 'PDF 원본 해시가 바뀌었습니다.' }
 $result=[ordered]@{status='CROPPED';source_sha256=$sourceHash;source_page=$Page;source_pages=$doc.PageCount;region_normalized=@($cropRegion);dpi=$Dpi;page_pixels=@($bitmap.Width,$bitmap.Height);crop_pixels=@($left,$top,$rect.Width,$rect.Height);output_sha256=(Hash $Output);visual_review='PENDING'}
 $json=$result | ConvertTo-Json -Depth 5
 if ($Receipt) { [IO.File]::WriteAllText([IO.Path]::GetFullPath($Receipt),$json,[Text.UTF8Encoding]::new($false)) }
 $json
} finally {
 if ($crop) { $crop.Dispose() };if ($bitmap) { $bitmap.Dispose() };if ($stream) { $stream.Dispose() };if ($random) { $random.Dispose() };if ($pdfPage) { $pdfPage.Dispose() }
}
