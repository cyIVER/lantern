// Rotate the CS2 server's RCON password. The new value is never printed --
// read it in the panel under the server's Startup tab.
$server = \App\Models\Server::where('name', 'LANtern CS2')->firstOrFail();
$egg    = $server->egg;

$var = $egg->variables()->where('env_variable', 'RCON_PASSWORD')->firstOrFail();

// alpha_dash|between:1,30
$new = substr(str_replace(['+', '/', '='], '', base64_encode(random_bytes(24))), 0, 24);

$row = \App\Models\ServerVariable::updateOrCreate(
    ['server_id' => $server->id, 'variable_id' => $var->id],
    ['variable_value' => $new],
);

$check = \App\Models\ServerVariable::where('server_id', $server->id)
    ->where('variable_id', $var->id)->value('variable_value');

echo 'rotated: ' . ($check === $new ? 'yes' : 'NO') . PHP_EOL;
echo 'length : ' . strlen($check) . PHP_EOL;
echo 'value  : (not printed -- see the panel, Startup tab)' . PHP_EOL;
