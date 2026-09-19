<?php
// Usage: php test_adapter.php /path/to/official/transcribe_interface.php URL TEMP_DIR
// The fixture server is local and accepts synthetic bytes only. Never use voicemail here.
require $argv[1];
require dirname(__DIR__).'/assets/app/resources/classes/transcribe_pbxctl_local.php';
$reflection = new ReflectionClass('transcribe_pbxctl_local');
$adapter = $reflection->newInstanceWithoutConstructor();
$reflection->getProperty('url')->setValue($adapter, $argv[2]);
$reflection->getProperty('lock_path')->setValue($adapter, $argv[3].'/inference.lock');
$adapter->set_path($argv[3]);
$adapter->set_filename('fixture.wav');
$depth=ob_get_level();
ob_start();
$text=$adapter->transcribe($argv[4] ?? 'text');
$output=ob_get_clean();
if ($output!=='' || ob_get_level()!==$depth) { throw new RuntimeException('Output leaked or buffer depth changed'); }
echo json_encode(['text'=>$text,'unsupported_format_empty'=>$adapter->transcribe('unsupported')==='','implements_interface'=>$adapter instanceof transcribe_interface]);
