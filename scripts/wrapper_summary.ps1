# Keep page text and coordinates in output artifacts unless -Full is requested.
function ConvertTo-LocalOcrSummary {
    param([AllowNull()]$Payload)
    if ($null -eq $Payload) { return $null }
    $fields = [ordered]@{}
    $names = @($Payload.PSObject.Properties.Name)
    foreach ($name in @(
        'ok', 'status', 'batch_coverage', 'skipped_inputs', 'count', 'smart',
        'display_summary', 'objective_outcome', 'execution_status', 'route',
        'output_files', 'page_count', 'expected_page_count', 'returned_page_count', 'cache_status',
        'source_file', 'source_path', 'job_id', 'job_key', 'manifest',
        'error_code', 'detail', 'http_status', 'recommendation', 'retryable', 'failure',
        'partial_output_files', 'failed_input', 'unprocessed_inputs', 'completed_count',
        'stage', 'engine', 'model_id', 'started_at', 'updated_at', 'deadline_at', 'timeout_sec',
        'active_jobs', 'active_jobs_count', 'active_owner', 'transport_error', 'detail_raw',
        'startup_output_tail', 'original_error_code', 'original_error', 'persistence_error'
    )) {
        if ($names -contains $name) { $fields[$name] = $Payload.$name }
    }
    foreach ($name in @('execution', 'coverage', 'quality')) {
        if ($names -contains $name) { $fields[$name] = [pscustomobject]@{ status = $Payload.$name.status } }
    }
    if ($names -contains 'pages') { $fields['page_count'] = @($Payload.pages).Count }
    if ($names -contains 'results') {
        $fields['results'] = @($Payload.results | ForEach-Object { ConvertTo-LocalOcrSummary $_ })
    }
    if ($names -contains 'ocr_result') { $fields['ocr_result'] = ConvertTo-LocalOcrSummary $Payload.ocr_result }
    return [pscustomobject]$fields
}
