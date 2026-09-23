<?php
declare(strict_types=1);
if (PHP_SAPI !== 'cli') { exit(1); }
umask(0077);
require_once __DIR__.'/summary.php';
$config = json_decode(file_get_contents('/etc/pbxctl/site.json'), true, 32, JSON_THROW_ON_ERROR);
require_once $config['web_root'].'/resources/require.php';
$database = new database;
if (in_array('--schema-check', $argv, true)) {
    // Safe during upgrades while FreeSWITCH and the model are stopped.
    $expected = [
        'v_voicemail_messages'=>['voicemail_message_uuid','voicemail_uuid','domain_uuid','created_epoch','message_transcription'],
        'v_voicemails'=>['voicemail_uuid','domain_uuid','voicemail_enabled','voicemail_transcription_enabled'],
    ];
    foreach ($expected as $table=>$columns) {
        $rows = $database->select('SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=:table', ['table'=>$table], 'all');
        if (array_diff($columns, array_column($rows ?: [], 'column_name'))) { exit(1); }
    }
    echo "Voicemail summary database interface: compatible\n";
    exit(0);
}
$ai = json_decode(file_get_contents('/var/lib/pbxctl/ai/config.json'), true, 16, JSON_THROW_ON_ERROR);
$dir = '/var/lib/pbxctl/ai';
$lock = fopen($dir.'/worker.lock', 'c');
if (!$lock || !flock($lock, LOCK_EX | LOCK_NB)) { exit(0); }

function pbx_ai_idle(): bool {
    try {
        $calls = event_socket::api('show channels count');
        if (!is_string($calls) || !preg_match('/(?:^|\n)\s*(\d+) total\./', $calls, $matches) || $matches[1] !== '0') { return false; }
        $memory = file_get_contents('/proc/meminfo');
        return preg_match('/MemAvailable:\s+(\d+) kB/', $memory, $matches) && (int)$matches[1] >= 512 * 1024;
    } catch (Throwable $e) { return false; }
}
function pbx_ai_atomic(string $path, array $data): void {
    $temporary = $path.'.'.bin2hex(random_bytes(5)).'.tmp';
    if (file_put_contents($temporary, json_encode($data, JSON_THROW_ON_ERROR), LOCK_EX) === false || !rename($temporary, $path)) {
        @unlink($temporary);
        throw new RuntimeException('Cannot persist worker state');
    }
}

if (in_array('--check', $argv, true)) {
    $calls = event_socket::api('show channels count');
    $fields = $database->select("SELECT column_name FROM information_schema.columns WHERE table_name='v_voicemail_messages' AND column_name='message_transcription'", [], 'all');
    $ch = curl_init('http://127.0.0.1:'.$config['ai_summary']['port'].'/health');
    curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER=>true,CURLOPT_TIMEOUT=>3,CURLOPT_PROXY=>'']);
    $body = curl_exec($ch);
    $health = curl_getinfo($ch, CURLINFO_RESPONSE_CODE) === 200 && is_string($body) && (json_decode($body, true)['status'] ?? '') === 'ok';
    $checks = ['database_interface'=>(bool)$fields,'phone_socket'=>is_string($calls) && preg_match('/\d+ total\./', $calls),'model_health'=>$health,'private_state_writable'=>is_writable($dir.'/originals'),'model_key_readable'=>is_readable('/etc/pbxctl/ai-key')];
    echo json_encode($checks, JSON_THROW_ON_ERROR)."\n";
    exit(in_array(false, array_map('boolval', $checks), true) ? 1 : 0);
}
if (!($config['ai_summary']['enabled'] ?? false) || !pbx_ai_idle()) { exit(0); }
$statePath = $dir.'/state.json';
$state = is_file($statePath) ? json_decode(file_get_contents($statePath), true, 32, JSON_THROW_ON_ERROR) : ['attempts'=>[]];

// Remove retained transcript copies after their voicemail is deleted.
foreach (array_slice(glob($dir.'/originals/*.json') ?: [], 0, 100) as $path) {
    $id = basename($path, '.json');
    if (!is_uuid($id)) { continue; }
    $exists = $database->select('SELECT voicemail_message_uuid FROM v_voicemail_messages WHERE voicemail_message_uuid=:id AND domain_uuid=:domain', ['id'=>$id,'domain'=>$ai['domain_uuid']], 'row');
    if (!$exists) { unlink($path); unset($state['attempts'][$id]); }
}

$rows = $database->select(
    'SELECT m.voicemail_message_uuid,m.message_transcription FROM v_voicemail_messages m '
    .'JOIN v_voicemails v USING(voicemail_uuid,domain_uuid) '
    .'WHERE m.domain_uuid=:domain AND m.created_epoch>=:since '
    .'AND v.voicemail_enabled=true AND v.voicemail_transcription_enabled=true '
    .'AND length(m.message_transcription) BETWEEN 32 AND 5000 '
    .'AND m.message_transcription NOT LIKE :prefix ORDER BY m.created_epoch ASC LIMIT 100',
    ['domain'=>$ai['domain_uuid'],'since'=>$ai['since'],'prefix'=>'AI voicemail notes (local;%'], 'all'
);

$done = 0; $failed = 0; $paused = 0;
foreach ($rows ?: [] as $row) {
    $id = $row['voicemail_message_uuid'];
    if (!is_uuid($id)) { continue; }
    $attempt = $state['attempts'][$id] ?? ['count'=>0,'time'=>0];
    if ($attempt['count'] >= 3 || time() - $attempt['time'] < 600) { continue; }
    if (!pbx_ai_idle()) { break; }
    $inference = fopen('/var/lib/pbxctl/transcribe/inference.lock', 'c');
    if (!$inference || !flock($inference, LOCK_EX | LOCK_NB)) { break; }
    $interrupted = false;
    try {
        $original = $row['message_transcription'];
        $notes = pbx_ai_request($original, 'pbx_ai_idle', $interrupted);
        if (!pbx_ai_idle()) { $interrupted = true; throw new RuntimeException('Phone activity'); }
        $rendered = pbx_ai_render($notes, $original);
        $backupPath = $dir.'/originals/'.$id.'.json';
        pbx_ai_atomic($backupPath, [
            'domain_uuid'=>$ai['domain_uuid'], 'voicemail_message_uuid'=>$id,
            'original'=>$original, 'rendered'=>$rendered,
            'created_at'=>gmdate('c'), 'model'=>'Qwen2.5-1.5B-Instruct Q4_K_M',
        ]);
        // A concurrent user edit or deletion wins over the generated notes.
        $database->execute('UPDATE v_voicemail_messages SET message_transcription=:rendered '
            .'WHERE voicemail_message_uuid=:id AND domain_uuid=:domain AND message_transcription=:original',
            ['rendered'=>$rendered,'id'=>$id,'domain'=>$ai['domain_uuid'],'original'=>$original]);
        $stored = $database->select('SELECT message_transcription FROM v_voicemail_messages WHERE voicemail_message_uuid=:id AND domain_uuid=:domain', ['id'=>$id,'domain'=>$ai['domain_uuid']], 'row');
        if (($stored['message_transcription'] ?? null) === $rendered) { $done++; }
        else { @unlink($backupPath); }
        unset($state['attempts'][$id]);
    } catch (Throwable $e) {
        if ($interrupted) { $paused++; }
        else { $failed++; $state['attempts'][$id] = ['count'=>$attempt['count']+1,'time'=>time()]; }
    } finally {
        flock($inference, LOCK_UN); fclose($inference);
        unset($original, $notes, $rendered);
    }
    break;
}
pbx_ai_atomic($statePath, $state);
if ($done || $failed || $paused) { echo "Local voicemail notes completed=$done failed=$failed paused=$paused\n"; }
exit($failed ? 1 : 0);
