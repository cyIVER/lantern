<?php
// Import (or re-import) an egg JSON from disk. Idempotent: an egg whose UUID is
// already known is UPDATED in place, so re-running never creates duplicates and
// existing servers keep working.
//
// Pelican ships no artisan command for this -- the panel only imports through a
// Filament file upload -- so this drives the same service the UI does.
//
//   docker compose cp ../eggs/lantern-minecraft.json panel:/tmp/egg.json
//   docker compose exec -T -e EGG_FILE=/tmp/egg.json panel php artisan tinker < bootstrap/import-egg.php

$path = getenv('EGG_FILE') ?: '/tmp/egg.json';

if (!is_file($path)) {
    echo "FATAL: no such file: {$path}" . PHP_EOL;
    return;
}

$json = json_decode(file_get_contents($path), true);
if (!is_array($json) || empty($json['uuid'])) {
    echo 'FATAL: not a valid egg export (no uuid)' . PHP_EOL;
    return;
}

$existing = \App\Models\Egg::where('uuid', $json['uuid'])->first();

// The service takes an UploadedFile. The final `true` puts it in test mode,
// which skips the is_uploaded_file() check that would reject a plain path.
$file = new \Illuminate\Http\UploadedFile($path, basename($path), 'application/json', null, true);

try {
    $egg = app(\App\Services\Eggs\Sharing\EggImporterService::class)->fromFile($file, $existing);
} catch (\Throwable $e) {
    echo 'FATAL: ' . get_class($e) . ': ' . $e->getMessage() . PHP_EOL;
    return;
}

echo ($existing ? 'UPDATED' : 'IMPORTED') . '  id=' . $egg->id
   . '  name=' . $egg->name
   . '  uuid=' . $egg->uuid
   . '  vars=' . $egg->variables()->count() . PHP_EOL;
