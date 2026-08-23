// Create the LANtern CS2 server. Idempotent: reports the existing one instead
// of creating a duplicate.
$owner = \App\Models\User::where('username', 'iveri')->first() ?? \App\Models\User::first();
$egg   = \App\Models\Egg::where('name', 'LANtern CS2')->firstOrFail();
$node  = \App\Models\Node::findOrFail(1);

$existing = \App\Models\Server::where('name', 'LANtern CS2')->first();
if ($existing) {
    echo 'ALREADY EXISTS  id=' . $existing->id . '  uuid=' . $existing->uuid
       . '  status=' . ($existing->status ?? 'installed') . PHP_EOL;
    return;
}

$game = \App\Models\Allocation::where('node_id', $node->id)->where('port', 27015)
    ->whereNull('server_id')->firstOrFail();
$tv = \App\Models\Allocation::where('node_id', $node->id)->where('port', 27020)
    ->whereNull('server_id')->first();

// RCON password must satisfy alpha_dash|between:1,30.
$rcon = substr(str_replace(['+', '/', '='], '', base64_encode(random_bytes(24))), 0, 24);

$data = [
    'name'        => 'LANtern CS2',
    'description' => 'CS2 for LAN play. MODE switches the active plugin set.',
    'owner_id'    => $owner->id,
    'egg_id'      => $egg->id,
    'allocation_id' => $game->id,
    'allocation_additional' => $tv ? [$tv->id] : [],
    'node_id'     => $node->id,

    'memory' => 8192,       // node allows 12288; leaves room for the game client
    'swap'   => 0,
    'disk'   => 60000,      // ~35 GB game + plugins + demos
    'io'     => 500,
    'cpu'    => 0,          // unlimited; 20 threads available
    'oom_killer' => false,

    'database_limit'   => 1,   // WeaponPaints needs MySQL if you enable skins
    'allocation_limit' => 4,
    'backup_limit'     => 2,

    'startup' => \Illuminate\Support\Arr::first($egg->startup_commands),
    'image'   => \Illuminate\Support\Arr::first($egg->docker_images),

    'environment' => [
        'MODE'            => 'competitive',
        'SERVER_NAME'     => 'LANtern',
        'MAX_PLAYERS'     => '12',
        'SRCDS_MAP'       => 'de_dust2',
        'SRCDS_APPID'     => '730',
        'TV_PORT'         => '27020',
        'RCON_ENABLED'    => '1',
        'RCON_PASSWORD'   => $rcon,
        'SERVER_PASSWORD' => '',
        'VAC_ENABLED'     => '1',
        'AUTO_UPDATE'     => '1',
        'STEAM_GSLT'      => '',
        'ENABLE_SKINS'    => '0',
        'BOT_QUOTA'       => '10',
        'BOT_DIFFICULTY'  => '2',
    ],

    'start_on_completion' => true,
];

try {
    $server = app(\App\Services\Servers\ServerCreationService::class)->handle($data);
    echo 'CREATED  id=' . $server->id . '  uuid=' . $server->uuid . PHP_EOL;
    echo 'connect  ' . $game->ip_alias . ':' . $game->port . PHP_EOL;
    echo 'rcon password stored in the panel under this server\'s Startup tab' . PHP_EOL;
    echo 'The ~35 GB steamcmd download is now running.' . PHP_EOL;
} catch (\Throwable $e) {
    echo 'FAILED: ' . get_class($e) . ': ' . $e->getMessage() . PHP_EOL;
    if (method_exists($e, 'errors')) {
        foreach ($e->errors() as $k => $v) {
            echo '   ' . $k . ': ' . implode('; ', (array) $v) . PHP_EOL;
        }
    }
}
