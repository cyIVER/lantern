$node = \App\Models\Node::find(1);

// Bind on 0.0.0.0 so Docker Desktop publishes to every interface; show the
// reserved LAN address to users via the alias.
$ip    = '0.0.0.0';
$alias = '192.168.0.115';

$ports = [
    27015, 27016, 27017, 27018, 27019,   // CS2 game ports (+ spares for extra servers)
    27020, 27021,                        // CSTV / SourceTV
    25565, 25566, 25567,                 // Minecraft
];

$made = 0; $skipped = 0;
foreach ($ports as $port) {
    $exists = \App\Models\Allocation::where('node_id', $node->id)
        ->where('ip', $ip)->where('port', $port)->exists();
    if ($exists) { $skipped++; continue; }

    \App\Models\Allocation::create([
        'node_id'  => $node->id,
        'ip'       => $ip,
        'ip_alias' => $alias,
        'port'     => $port,
    ]);
    $made++;
}

echo "created: $made  skipped(existing): $skipped" . PHP_EOL;
echo "total allocations on node: " . \App\Models\Allocation::where('node_id', $node->id)->count() . PHP_EOL;
foreach (\App\Models\Allocation::where('node_id', $node->id)->orderBy('port')->get() as $a) {
    echo "  " . $a->ip . ":" . $a->port . "  alias=" . $a->ip_alias . "  server=" . ($a->server_id ?? 'free') . PHP_EOL;
}
