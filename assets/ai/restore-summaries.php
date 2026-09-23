<?php
declare(strict_types=1);
if (PHP_SAPI !== 'cli') { exit(1); }
$config = json_decode(file_get_contents('/etc/pbxctl/site.json'), true, 32, JSON_THROW_ON_ERROR);
require_once $config['web_root'].'/resources/require.php';
$ai = json_decode(file_get_contents('/var/lib/pbxctl/ai/config.json'), true, 16, JSON_THROW_ON_ERROR);
$lock = fopen('/var/lib/pbxctl/ai/worker.lock', 'c');
if (!$lock || !flock($lock, LOCK_EX | LOCK_NB)) { exit(75); }
$database = new database;
$count = 0;
foreach (glob('/var/lib/pbxctl/ai/originals/*.json') ?: [] as $path) {
    $record = json_decode(file_get_contents($path), true, 16, JSON_THROW_ON_ERROR);
    if (!is_uuid($record['voicemail_message_uuid'] ?? '') || ($record['domain_uuid'] ?? '') !== $ai['domain_uuid']) { continue; }
    $database->execute('UPDATE v_voicemail_messages SET message_transcription=:original '
        .'WHERE voicemail_message_uuid=:id AND domain_uuid=:domain AND message_transcription=:rendered',
        ['original'=>$record['original'],'rendered'=>$record['rendered'],'id'=>$record['voicemail_message_uuid'],'domain'=>$ai['domain_uuid']]);
    $count++;
}
echo "Original transcript restoration checked $count records.\n";
