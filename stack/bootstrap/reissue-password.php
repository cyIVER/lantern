<?php
// Reissue a panel account's password.
//
// Written for the case where a password has leaked -- it was in a plaintext
// file that got read into a terminal -- so the point is to invalidate the old
// one, not to be convenient.
//
// The new password is generated here and returned rather than printed by the
// caller's shell, so it does not land in a scrollback or a transcript. The
// caller writes it to a mode-600 file and the operator hands it over out of
// band.
//
// Run it through tinker with require rather than pasting the body into
// --execute: tinker echoes the source it is given, so an inline script that
// contains the words it checks for will appear to have succeeded whatever
// actually happened.
//
//   USERNAME=scotland php artisan tinker --execute='require "/tmp/reissue.php";'

$username = getenv('USERNAME') ?: 'scotland';

$user = \App\Models\User::where('username', $username)->first();
if (! $user) {
    echo "REISSUE-FAIL no such user: {$username}" . PHP_EOL;
    return;
}

// Alphanumeric only. This gets typed by hand by someone on another machine,
// possibly on a different keyboard layout, and the panel's own validation
// rejects some punctuation. Length carries the entropy instead.
$alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
$password = '';
for ($i = 0; $i < 24; $i++) {
    $password .= $alphabet[random_int(0, strlen($alphabet) - 1)];
}

$user->password = \Illuminate\Support\Facades\Hash::make($password);
$user->save();

// Read it back rather than trusting save(). A silent failure here leaves the
// leaked password working, which is the whole thing being fixed.
$user->refresh();
if (! \Illuminate\Support\Facades\Hash::check($password, $user->password)) {
    echo 'REISSUE-FAIL the new password does not verify' . PHP_EOL;
    return;
}

echo 'REISSUE-OK ' . $user->username . ' ' . $user->email . ' ' . $password . PHP_EOL;
