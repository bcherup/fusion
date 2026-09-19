<?php
// Private background worker: new, opted-in voicemail only; never log message content.
if (PHP_SAPI !== 'cli') { exit(1); }
umask(0077);
$config = json_decode(file_get_contents('/etc/pbxctl/site.json'), true, 512, JSON_THROW_ON_ERROR);
require_once $config['web_root'] . '/resources/require.php';
$stateDir = '/var/lib/pbxctl/transcribe';
$lock = fopen($stateDir . '/worker.lock', 'c');
if (!$lock || !flock($lock, LOCK_EX | LOCK_NB)) { exit(0); }
$statePath = $stateDir . '/state.json';
$state = json_decode(file_get_contents($statePath), true, 512, JSON_THROW_ON_ERROR);
$database = new database;
$sql = "SELECT m.voicemail_message_uuid, m.voicemail_uuid, m.domain_uuid, d.domain_name, v.voicemail_id "
     . "FROM v_voicemail_messages m JOIN v_voicemails v USING(voicemail_uuid,domain_uuid) "
     . "JOIN v_domains d ON d.domain_uuid=m.domain_uuid "
     . "WHERE v.voicemail_enabled=true AND v.voicemail_transcription_enabled=true "
     . "AND m.domain_uuid=:domain_uuid AND m.created_epoch >= :since "
     . "AND COALESCE(m.message_transcription,'')='' "
     . "ORDER BY m.created_epoch ASC LIMIT 1000";
$rows = $database->select($sql, ['domain_uuid' => $state['domain_uuid'], 'since' => $state['since']], 'all');
$done = 0; $failed = 0;
foreach ($rows ?: [] as $row) {
    $id = $row['voicemail_message_uuid'];
    if (!is_uuid($id) || !ctype_digit((string)$row['voicemail_id'])) { continue; }
    $prior = $state['attempts'][$id] ?? ['count' => 0, 'time' => 0];
    if ($prior['count'] >= 3 || time() - $prior['time'] < 600) { continue; }
    $settings = new settings(['database' => $database, 'domain_uuid' => $row['domain_uuid']]);
    if (!$settings->get('transcribe', 'enabled', false) || $settings->get('transcribe', 'engine') !== 'pbxctl_local') { break; }
    $base = realpath($settings->get('switch', 'voicemail', '/var/lib/freeswitch/storage/voicemail'));
    $dir = $base . '/default/' . $row['domain_name'] . '/' . $row['voicemail_id'];
    $name = null;
    foreach (['wav', 'mp3'] as $ext) {
        $candidate = realpath($dir . '/msg_' . $id . '.' . $ext);
        if ($base && $candidate && str_starts_with($candidate, $base . '/') && is_readable($candidate)) {
            $dir = dirname($candidate); $name = basename($candidate); break;
        }
    }
    if ($name === null) { continue; } // Originals remain untouched; filesystem storage is required.
    try {
        $transcribe = new transcribe($settings);
        $transcribe->audio_path = $dir;
        $transcribe->audio_filename = $name;
        $message = trim((string)$transcribe->transcribe('text'));
        if ($message === '' || str_starts_with($message, 'Error:')) { throw new RuntimeException('transcription unavailable'); }
        $database->execute("UPDATE v_voicemail_messages SET message_transcription=:message "
            . "WHERE voicemail_message_uuid=:id AND domain_uuid=:domain AND COALESCE(message_transcription,'')=''",
            ['message' => $message, 'id' => $id, 'domain' => $row['domain_uuid']]);
        unset($state['attempts'][$id]);
        $done++;
    } catch (Throwable $e) {
        $state['attempts'][$id] = ['count' => $prior['count'] + 1, 'time' => time()];
        $failed++;
    }
    unset($message, $transcribe);
    break; // One message at a time; the timer handles the next.
}
$tmp = $statePath . '.tmp';
file_put_contents($tmp, json_encode($state, JSON_THROW_ON_ERROR), LOCK_EX);
rename($tmp, $statePath);
if ($done || $failed) { echo "Voicemail transcription completed=$done failed=$failed\n"; }
exit($failed ? 1 : 0);
