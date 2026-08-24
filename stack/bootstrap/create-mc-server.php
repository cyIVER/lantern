<?php
// Create the LANtern Minecraft server. Idempotent: reports the existing one
// instead of creating a duplicate.
//
// Requires CURSEFORGE_API_KEY in the environment -- pass it through with
//   docker compose exec -T -e CURSEFORGE_API_KEY="$KEY" panel php artisan tinker < ...
// so the key never lands in a file inside the container.

$key = getenv('CURSEFORGE_API_KEY');
if (!$key) {
    echo 'FATAL: CURSEFORGE_API_KEY is not set. See docs/SECRETS.md.' . PHP_EOL;
    return;
}

$owner = \App\Models\User::where('username', 'iveri')->first() ?? \App\Models\User::first();
$egg   = \App\Models\Egg::where('name', 'LANtern Minecraft')->firstOrFail();
$node  = \App\Models\Node::findOrFail(1);

$existing = \App\Models\Server::where('name', 'LANtern Minecraft')->first();
if ($existing) {
    echo 'ALREADY EXISTS  id=' . $existing->id . '  uuid=' . $existing->uuid
       . '  status=' . ($existing->status ?? 'installed') . PHP_EOL;
    return;
}

// ---------------------------------------------------------------- node limits
// Pelican caps the SUM of every server's memory on a node, whether or not the
// server is running. LANtern deliberately runs one game at a time (see
// `lantern use`), so the node is intentionally overcommitted relative to the
// host's 32 GB -- the ceiling has to fit CS2 + Minecraft on paper even though
// only one is ever resident.
$needMemory = 8192 + 11264 + 2048;   // CS2 + Minecraft + slack for a third game
$needDisk   = 120000 + 30000 + 20000;

$changed = [];
if ($node->memory < $needMemory) { $changed[] = "memory {$node->memory} -> {$needMemory}"; $node->memory = $needMemory; }
if ($node->disk   < $needDisk)   { $changed[] = "disk {$node->disk} -> {$needDisk}";       $node->disk   = $needDisk; }
if ($changed) {
    $node->save();
    echo 'node raised: ' . implode(', ', $changed) . PHP_EOL;
}

// --------------------------------------------------------------- allocations
// Game port plus a second one for RCON, which the control UI needs to read the
// roster. Wings only publishes ports that are allocated.
$game = \App\Models\Allocation::where('node_id', $node->id)->where('port', 25565)
    ->whereNull('server_id')->first();
if (!$game) { echo 'FATAL: port 25565 is not free on this node' . PHP_EOL; return; }

$rconAlloc = \App\Models\Allocation::where('node_id', $node->id)->where('port', 25575)
    ->whereNull('server_id')->first();
if (!$rconAlloc) {
    $rconAlloc = \App\Models\Allocation::create([
        'node_id' => $node->id, 'ip' => '0.0.0.0', 'port' => 25575, 'ip_alias' => null,
    ]);
    echo 'created allocation 0.0.0.0:25575 for rcon' . PHP_EOL;
}

// alpha_dash, so no base64 padding or slashes.
$rcon = substr(str_replace(['+', '/', '='], '', base64_encode(random_bytes(24))), 0, 24);

$data = [
    'name'        => 'LANtern Minecraft',
    'description' => 'All the Mods 10 (NeoForge 1.21.1), pinned to an exact CurseForge file id.',
    'owner_id'    => $owner->id,
    'egg_id'      => $egg->id,
    'allocation_id' => $game->id,
    'allocation_additional' => [$rconAlloc->id],
    'node_id'     => $node->id,

    // 11264 MiB total => 9216 MiB heap after 2048 MiB of headroom.
    //
    // Slightly under the 10 GB the pack authors suggest, deliberately. Aikar's
    // flags include -XX:+AlwaysPreTouch, so the JVM commits the entire heap at
    // startup; a 10 GB heap in an 11 GB container left ~1 GB for the metaspace
    // of 455 mods plus code cache, threads and direct buffers, and the server
    // pinned itself at the cgroup limit and never finished loading.
    'memory' => 11264,
    'swap'   => 0,
    'disk'   => 30000,      // 1.1 GB pack extracted to ~6 GB, plus world growth
    'io'     => 500,
    'cpu'    => 0,          // unlimited; 20 threads available
    'oom_killer' => false,

    'database_limit'   => 0,
    'allocation_limit' => 4,
    'backup_limit'     => 3,

    'startup' => \Illuminate\Support\Arr::first($egg->startup_commands),
    'image'   => \Illuminate\Support\Arr::first($egg->docker_images),

    'environment' => [
        'CF_PROJECT_ID'       => '925200',    // All the Mods 10
        'CF_FILE_ID'          => '8649077',   // ATM10 8.0 -- pinned, see docs/MINECRAFT.md
        'CURSEFORGE_API_KEY'  => $key,
        'MAX_PLAYERS'         => '8',
        'MOTD'                => 'LANtern - All the Mods 10',
        'DIFFICULTY'          => 'normal',
        'VIEW_DISTANCE'       => '8',
        'SIMULATION_DISTANCE' => '6',
        'ONLINE_MODE'         => '1',
        'PVP'                 => '1',
        'WHITELIST'           => '0',
        'RCON_PASSWORD'       => $rcon,
        'RCON_PORT'           => '25575',
        'JVM_HEADROOM_MB'     => '2048',
    ],

    'start_on_completion' => false,
];

try {
    $server = app(\App\Services\Servers\ServerCreationService::class)->handle($data);
} catch (\Throwable $e) {
    echo 'FATAL: ' . get_class($e) . ': ' . $e->getMessage() . PHP_EOL;
    return;
}

echo 'CREATED  id=' . $server->id . '  uuid=' . $server->uuid . PHP_EOL;
echo 'memory   ' . $server->memory . ' MiB (heap ' . ($server->memory - 1024) . ' MiB)' . PHP_EOL;
echo 'ports    25565 game, 25575 rcon' . PHP_EOL;
echo 'rcon     written to the server environment; read it back with' . PHP_EOL;
echo '         bootstrap/mc-status.sh' . PHP_EOL;
echo PHP_EOL . 'Install is now downloading a 1.1 GB server pack. Watch it with:' . PHP_EOL;
echo '  docker compose logs -f wings | grep -i install' . PHP_EOL;
