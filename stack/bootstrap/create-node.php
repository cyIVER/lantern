$existing = \App\Models\Node::where('name', 'lantern')->first();
if ($existing) {
    echo "NODE ALREADY EXISTS id=" . $existing->id . PHP_EOL;
    $node = $existing;
} else {
    $node = new \App\Models\Node();
    $node->name = 'lantern';
    $node->description = 'LANtern host - Docker Desktop / WSL2';
    $node->public = true;
    $node->fqdn = '192.168.0.115';
    $node->scheme = 'http';
    $node->behind_proxy = false;
    // WSL2 caps the VM around 15 GB; leave headroom for the host and the game client.
    $node->memory = 12288;
    $node->memory_overallocate = 0;
    // Ubuntu's vhdx sits on E: with ~686 GB free.
    $node->disk = 400000;
    $node->disk_overallocate = 0;
    $node->cpu = 0;
    $node->cpu_overallocate = 0;
    $node->upload_size = 256;
    $node->daemon_base = '/var/lib/pelican/volumes';
    $node->daemon_sftp = 2022;
    $node->daemon_listen = 8080;
    $node->daemon_connect = 8080;
    $node->maintenance_mode = false;
    $node->save();
    echo "NODE CREATED id=" . $node->id . PHP_EOL;
}
echo "-----BEGIN CONFIG-----" . PHP_EOL;
echo $node->getYamlConfiguration();
echo "-----END CONFIG-----" . PHP_EOL;
