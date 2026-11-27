param([string]$MaimiRequest)
# Trusted application helper. No document contents are written to disk.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$request = $null; $app = $null; $doc = $null; $opened = $false; $automationCreated = $false
$mutex=$null;$mutexOwned=$false
$openAttempted=$false
$result = [ordered]@{schema='maimi-wps-read/v1';status='FAILED';error='';text='';truncated=$false;plaintext_files_created=0;existing_wps_closed=$false;application_exit_requested=$false;cleanup_scope='OWN_DOCUMENT_AND_HELPER_PLAINTEXT_ONLY';wps_internal_cache_audited=$false;cleanup_confirmed=$true;mode='';units_read=0}
$text = New-Object System.Text.StringBuilder
function Release-Com($value) {
    if ($null -ne $value -and [Runtime.InteropServices.Marshal]::IsComObject($value)) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($value) }
}
function Add-Text([string]$value) {
    $remaining = [int]$request.maxChars - $text.Length
    if ($remaining -le 0) { $result.truncated = $true; return $false }
    if ($value.Length -gt $remaining) { [void]$text.Append($value.Substring(0,$remaining)); $result.truncated=$true; return $false }
    [void]$text.Append($value); return $true
}
function Exact-Path($candidate) {
    try { return [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetFullPath([string]$candidate),$file) } catch { return $false }
}
try {
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $mutex=New-Object Threading.Mutex($false,('Local\MaimiWpsRead-'+$sid))
    try{$mutexOwned=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$mutexOwned=$true;throw 'WPS_PREVIOUS_READ_INTERRUPTED'}
    if(-not $mutexOwned){throw 'WPS_READER_BUSY'}
    $raw = $MaimiRequest
    if ($raw.Length -gt 16384) { throw 'INVALID_REQUEST' }
    $request = $raw | ConvertFrom-Json
    foreach($rule in @(@('start',1,1000000),@('count',1,100),@('maxChars',100,100000),@('sheet',1,256),@('columns',1,100))) {
        $v=$request.($rule[0]); if($null -eq $v -or $v -ne [int]$v -or $v -lt $rule[1] -or $v -gt $rule[2]) { throw 'INVALID_REQUEST' }
    }
    $file=[IO.Path]::GetFullPath([string]$request.file)
    if($file -notmatch '^[A-Za-z]:\\' -or $file.Substring(2).Contains(':')) { throw 'LOCAL_ABSOLUTE_FILE_REQUIRED' }
    $item=Get-Item -LiteralPath $file
    if($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $item.Length -gt 268435456) { throw 'FILE_TYPE_OR_SIZE_REFUSED' }
    # Refuse linked ancestors as well; the chosen local path is the access boundary.
    $parent=$item.Directory
    while($null -ne $parent) { if(($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'LINKED_FILE_PATH_REFUSED' }; $parent=$parent.Parent }
    $kind=switch -Regex ($item.Extension.ToLowerInvariant()) {
        '^\.(doc|docx|wps)$' {'word';break}; '^\.(xls|xlsx|et)$' {'sheet';break}
        '^\.(ppt|pptx|dps)$' {'slides';break}; '^\.pdf$' {'pdf';break}; default {throw 'UNSUPPORTED_FILE_TYPE'}
    }
    $progId=@{word='KWPS.Application';sheet='KET.Application';slides='KWPP.Application';pdf='KPdf.Application'}[$kind]
    try { $app=[Runtime.InteropServices.Marshal]::GetActiveObject($progId) } catch { $app=$null }
    if($null -ne $app) {
        # Only compare names with the exact selection; never return other document names.
        $collection=switch($kind) {'word'{$app.Documents};'sheet'{$app.Workbooks};'slides'{$app.Presentations};'pdf'{$app.Documents}}
        try {
            $n=[int]$collection.Count
            if($n -gt 128) { throw 'TOO_MANY_OPEN_DOCUMENTS' }
            for($i=1;$i -le $n;$i++) {
                $candidate=$collection.Item($i)
                if(Exact-Path $candidate.FullName) { $doc=$candidate; break }
                Release-Com $candidate
            }
        } finally { Release-Com $collection }
        if($null -ne $doc){$result.mode='EXISTING_SELECTED_DOCUMENT_READ_ONLY'}
    }
    if($null -eq $doc) {
        if($kind -eq 'pdf'){throw 'WPS_PDF_PAGE_INTERFACE_UNSUPPORTED'}
        Release-Com $app;$app=$null
        if($null -eq [type]::GetTypeFromProgID($progId)){throw 'WPS_COMPONENT_UNAVAILABLE'}
        $app=New-Object -ComObject $progId
        $automationCreated=$true
        $collection=switch($kind){'word'{$app.Documents};'sheet'{$app.Workbooks};'slides'{$app.Presentations}}
        try{$empty=([int]$collection.Count -eq 0)}finally{Release-Com $collection}
        # COM can reuse an interactive instance. Never change security on that instance.
        # PowerPoint's object model does not expose UserControl; its factory
        # context must still be empty and hidden. Never Quit this shared app.
        if(-not $empty -or ($kind -ne 'slides' -and ($null -eq $app.UserControl -or [bool]$app.UserControl)) -or $null -eq $app.Visible -or [bool]$app.Visible){throw 'OPEN_SELECTED_FILE_IN_WPS'}
        try {$oldSecurity=[int]$app.AutomationSecurity;$app.AutomationSecurity=3;if([int]$app.AutomationSecurity -ne 3){throw 'unsupported'}}catch{throw 'OPEN_SELECTED_FILE_IN_WPS'}
        try {
            $openAttempted=$true
            switch($kind){
                'word' {$missing=[Type]::Missing;$doc=$app.Documents.Open($file,$false,$true,$false,$missing,$missing,$false,$missing,$missing,$missing,$missing,$false)}
                'sheet' {$doc=$app.Workbooks.Open($file,0,$true)}
                'slides' {$doc=$app.Presentations.Open($file,-1,0,0)}
            }
        }finally{try{$app.AutomationSecurity=$oldSecurity}catch{$result.cleanup_confirmed=$false}}
        if(-not(Exact-Path $doc.FullName)){throw 'OPENED_FILE_IDENTITY_MISMATCH'}
        if($null -eq $doc.ReadOnly -or -not [bool]$doc.ReadOnly){throw 'READ_ONLY_OPEN_NOT_CONFIRMED'}
        # Some WPS builds merge windows across instances. A returned document
        # belongs to this read only when its controlling Application is the
        # same COM identity as our previously empty automation context.
        $docApp=$null;$appIdentity=[IntPtr]::Zero;$docAppIdentity=[IntPtr]::Zero
        try{
            $docApp=$doc.Application
            $appIdentity=[Runtime.InteropServices.Marshal]::GetIUnknownForObject($app)
            $docAppIdentity=[Runtime.InteropServices.Marshal]::GetIUnknownForObject($docApp)
            if($appIdentity -ne $docAppIdentity){throw 'OPENED_DOCUMENT_OWNER_UNCONFIRMED'}
        }finally{
            if($appIdentity -ne [IntPtr]::Zero){[void][Runtime.InteropServices.Marshal]::Release($appIdentity)}
            if($docAppIdentity -ne [IntPtr]::Zero){[void][Runtime.InteropServices.Marshal]::Release($docAppIdentity)}
            Release-Com $docApp
        }
        $opened=$true
        $result.mode='AUTOMATION_READ_ONLY_DOCUMENT'
    }
    $start=[int]$request.start; $count=[int]$request.count
    switch($kind) {
        'word' {
            $parts=$doc.Paragraphs
            try { $total=[int]$parts.Count; $end=[Math]::Min($total,$start+$count-1);$lastProcessed=$start-1
                for($i=$start;$i -le $end;$i++) { $part=$parts.Item($i); $range=$null
                    try { $range=$part.Range; $s=[int]$range.Start; $e=[int]$range.End; $take=[Math]::Min($e,$s+([int]$request.maxChars-$text.Length)); $range.SetRange($s,$take); $keep=Add-Text ([string]$range.Text); $result.units_read++;$lastProcessed=$i; if($take -lt $e){$result.truncated=$true}; if(-not $keep -or $text.Length -ge $request.maxChars){break} }
                    finally { Release-Com $range; Release-Com $part }
                }; if($lastProcessed -lt $total){$result.truncated=$true}
            } finally { Release-Com $parts }
        }
        'sheet' {
            $sheet=$doc.Worksheets.Item([int]$request.sheet);$used=$null
            try { $used=$sheet.UsedRange; $rowCount=[int]$used.Rows.Count; $colCount=[int]$used.Columns.Count; $lastRow=[int]$used.Row+$rowCount-1; $lastCol=[int]$used.Column+$colCount-1
                $lastProcessed=$start-1;$end=[Math]::Min($lastRow,$start+$count-1);$columns=[Math]::Min($lastCol,[int]$request.columns)
                for($r=$start;$r -le $end;$r++){for($c=1;$c -le $columns;$c++){ $cell=$sheet.Cells.Item($r,$c);try{$value=[string]$cell.Text}finally{Release-Com $cell};if($value.Length -gt 32768){$value=$value.Substring(0,32768);$result.truncated=$true};if(-not(Add-Text($value+$(if($c -lt $columns){"`t"}else{"`n"})))){break} };$result.units_read++;$lastProcessed=$r;if($text.Length -ge $request.maxChars){break}}
                if($lastProcessed -lt $lastRow -or $columns -lt $lastCol){$result.truncated=$true}
            } finally { Release-Com $used;Release-Com $sheet }
        }
        'slides' {
            $lastProcessed=$start-1;$total=[int]$doc.Slides.Count;$end=[Math]::Min($total,$start+$count-1)
            for($s=$start;$s -le $end;$s++){ $slide=$doc.Slides.Item($s)
                try { [void](Add-Text("[Slide $s]`n"));$shapes=$slide.Shapes;$shapeCount=[int]$shapes.Count
                    try { for($i=1;$i -le [Math]::Min(100,$shapeCount);$i++){ $shape=$shapes.Item($i)
                        try { if($shape.HasTextFrame -ne 0){$frame=$shape.TextFrame;$range=$null;try{$range=$frame.TextRange;$length=[int]$range.Length;$take=[Math]::Min($length,[Math]::Max(1,$request.maxChars-$text.Length));$part=$range.Characters(1,$take);try{[void](Add-Text([string]$part.Text+"`n"))}finally{Release-Com $part};if($take -lt $length){$result.truncated=$true}}finally{Release-Com $range;Release-Com $frame}}
                            if($shape.HasTable -ne 0){$table=$shape.Table;try{$rows=[Math]::Min(50,[int]$table.Rows.Count);$cols=[Math]::Min(30,[int]$table.Columns.Count);for($r=1;$r -le $rows;$r++){for($c=1;$c -le $cols;$c++){$cell=$table.Cell($r,$c);$cellShape=$cell.Shape;$frame=$cellShape.TextFrame;$range=$frame.TextRange;try{$part=$range.Characters(1,[Math]::Min([int]$range.Length,[Math]::Max(1,$request.maxChars-$text.Length)));try{[void](Add-Text([string]$part.Text+"`t"))}finally{Release-Com $part}}finally{Release-Com $range;Release-Com $frame;Release-Com $cellShape;Release-Com $cell};if($text.Length -ge $request.maxChars){if($c -lt $cols){$result.truncated=$true};break}};if($text.Length -ge $request.maxChars){if($r -lt $rows){$result.truncated=$true};break}};if($rows -lt $table.Rows.Count -or $cols -lt $table.Columns.Count){$result.truncated=$true}}finally{Release-Com $table}}
                        } finally {Release-Com $shape};if($text.Length -ge $request.maxChars){if($i -lt $shapeCount){$result.truncated=$true};break}
                    };if($shapeCount -gt 100){$result.truncated=$true} }finally{Release-Com $shapes}
                }finally{Release-Com $slide};$result.units_read++;$lastProcessed=$s;if($text.Length -ge $request.maxChars){break}
            };if($lastProcessed -lt $total){$result.truncated=$true}
        }
        'pdf' {
            # Never read DocumentText for an unbounded PDF. Interface support differs by WPS version.
            try {$total=[int]$doc.PageCount;if($total -lt 1){throw 'unsupported'};$end=[Math]::Min($total,$start+$count-1)
                for($p=$start;$p -le $end;$p++){$value=[string]$doc.PageText($p);if($value.Length -gt 1000000){throw 'PAGE_TEXT_LIMIT'};[void](Add-Text($value+"`n"));$result.units_read++;if($text.Length -ge $request.maxChars){break}};if($end -lt $total){$result.truncated=$true}
            }catch{throw 'WPS_PDF_PAGE_INTERFACE_UNSUPPORTED'}
        }
    }
    $result.status='READ';$result.text=$text.ToString()
} catch {
    $allowed=@('WPS_READER_BUSY','WPS_PREVIOUS_READ_INTERRUPTED','INVALID_REQUEST','LOCAL_ABSOLUTE_FILE_REQUIRED','FILE_TYPE_OR_SIZE_REFUSED','LINKED_FILE_PATH_REFUSED','UNSUPPORTED_FILE_TYPE','TOO_MANY_OPEN_DOCUMENTS','OPEN_SELECTED_FILE_IN_WPS','WPS_COMPONENT_UNAVAILABLE','WPS_INSTANCE_OWNERSHIP_UNKNOWN','OPENED_FILE_IDENTITY_MISMATCH','OPENED_DOCUMENT_OWNER_UNCONFIRMED','READ_ONLY_OPEN_NOT_CONFIRMED','WPS_PDF_PAGE_INTERFACE_UNSUPPORTED')
    $code=$_.Exception.Message
    $result.error=if($code -in $allowed){$code}else{'WPS_READ_FAILED_OR_ACCESS_DENIED'}
    $result.text=''
} finally {
    if($openAttempted -and -not $opened){$result.cleanup_confirmed=$false}
    if($opened -and $null -ne $doc) { try { if($kind -eq 'slides'){$doc.Close()}else{$doc.Close($false)} } catch {$result.cleanup_confirmed=$false} }
    Release-Com $doc
    # WPS can share a UI broker across COM instances. Never Quit the application.
    Release-Com $app
    if($mutexOwned){$mutex.ReleaseMutex()};if($null -ne $mutex){$mutex.Dispose()}
}
[Console]::Out.WriteLine(($result | ConvertTo-Json -Depth 5 -Compress))
