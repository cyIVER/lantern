// Provision a MySQL database for WeaponPaints through Pelican, so it also
// appears in the panel UI under the server's Databases tab.
//
// The database host points at the same MariaDB container the panel uses. That
// is fine on a single-box LAN setup: Pelican creates a separate database and a
// scoped user, it does not share the panel's own schema or credentials.

$server = \App\Models\Server::where('name', 'LANtern CS2')->firstOrFail();
$node   = $server->node;

$rootPw = env('DB_PASSWORD');
if (!$rootPw) { echo 'FAILED: DB_PASSWORD not readable from env' . PHP_EOL; return; }

$host = \App\Models\DatabaseHost::firstWhere('host', 'database');
if (!$host) {
    $host = \App\Models\DatabaseHost::create([
        'name'          => 'LANtern MariaDB',
        'host'          => 'database',   // compose service name, panel-internal
        'port'          => 3306,
        'username'      => 'pelican',
        'password'      => $rootPw,
        'max_databases' => null,
    ]);
    echo 'created database host id=' . $host->id . PHP_EOL;
} else {
    echo 'database host already exists id=' . $host->id . PHP_EOL;
}

// Restrict the host to our node so it is selectable for this server.
if (method_exists($host, 'nodes')) {
    $host->nodes()->syncWithoutDetaching([$node->id]);
    echo 'linked to node ' . $node->id . PHP_EOL;
}

$existing = \App\Models\Database::where('server_id', $server->id)->first();
if ($existing) {
    echo 'database already exists: ' . $existing->database . PHP_EOL;
    return;
}

try {
    $db = app(\App\Services\Databases\DatabaseManagementService::class)->create($server, [
        // Pelican requires the s{server_id}_ prefix; it namespaces databases
        // per server on a shared host.
        'database'       => 's' . $server->id . '_weaponpaints',
        'remote'         => '%',
        'database_host_id' => $host->id,
        'max_connections' => 0,
    ]);
    echo 'CREATED database : ' . $db->database . PHP_EOL;
    echo 'username         : ' . $db->username . PHP_EOL;
    echo 'host             : ' . $host->host . ':' . $host->port . PHP_EOL;
    echo '(password is in the panel under the server Databases tab)' . PHP_EOL;
} catch (\Throwable $e) {
    echo 'FAILED: ' . get_class($e) . ': ' . $e->getMessage() . PHP_EOL;
}
