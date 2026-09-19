<?php
/** Local voicemail adapter. Installed outside FusionPBX; no vendor-file edits. */
class transcribe_pbxctl_local implements transcribe_interface {
    public $api_model = 'base.en';
    public $audio_channels = 1;
    private $path = '';
    private $filename = '';
    private $audio_string = '';
    private $audio_mime_type = 'audio/wav';
    private $url;
    private $lock_path = '/var/lib/pbxctl/transcribe/inference.lock';
    public function __construct($settings) {
        $config = json_decode(file_get_contents('/etc/pbxctl/site.json'), true, 512, JSON_THROW_ON_ERROR);
        $port = filter_var($config['whisper_port'], FILTER_VALIDATE_INT, ['options' => ['min_range'=>1024, 'max_range'=>65535]]);
        if (!$port) { throw new RuntimeException('Invalid local transcription port'); }
        $this->url = 'http://127.0.0.1:' . $port . '/inference';
    }
    public function set_path(string $v) { $this->path = $v; }
    public function set_filename(string $v) { $this->filename = $v; }
    public function set_audio_string(string $v) { $this->audio_string = $v; }
    public function set_audio_mime_type(string $v) { $this->audio_mime_type = $v; }
    public function set_format(string $v) {}
    public function set_language(string $v) {}
    public function set_translate(string $v) {}
    public function set_message(string $v) {}
    public function is_language_enabled(): bool { return false; }
    public function is_translate_enabled(): bool { return false; }
    public function get_languages(): array { return ['en'=>'English']; }
    public function set_model(string $v): void {}
    public function get_models(): array { return ['base.en'=>'Whisper base.en (local)']; }
    public function transcribe(?string $output_type = 'text'): string {
        // Text for voicemail; timestamped single-stream JSON for native app callers.
        // Speaker separation is not inferred from a mono recording.
        if (!in_array($output_type, ['text', 'json'], true)) { return ''; }
        $lock = null;
        try {
            if ($this->filename !== basename($this->filename)) { return ''; }
            $file = $this->path . '/' . $this->filename;
            if (is_file($file) && is_readable($file)) {
                if (filesize($file) > 50 * 1024 * 1024) { return ''; }
                $audio = new CURLFile($file, $this->audio_mime_type, $this->filename);
            } elseif ($this->audio_string !== '' && strlen($this->audio_string) <= 50 * 1024 * 1024) {
                $audio = new CURLStringFile($this->audio_string, $this->filename ?: 'message.wav', $this->audio_mime_type);
            } else { return ''; }
            $lock = fopen($this->lock_path, 'c');
            // A busy worker is retried later; never block an email queue indefinitely.
            if (!$lock || !flock($lock, LOCK_EX | LOCK_NB)) { return ''; }
            $ch = curl_init($this->url);
            curl_setopt_array($ch, [
                CURLOPT_POST=>true,
                CURLOPT_POSTFIELDS=>['file'=>$audio, 'response_format'=>$output_type === 'json' ? 'verbose_json' : 'text', 'language'=>'en'],
                CURLOPT_RETURNTRANSFER=>true,
                CURLOPT_CONNECTTIMEOUT=>5,
                CURLOPT_TIMEOUT=>600,
                CURLOPT_FOLLOWLOCATION=>false,
                CURLOPT_PROXY=>'',
                CURLOPT_VERBOSE=>false,
            ]);
            $result = curl_exec($ch);
            $status = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
            $failed = curl_errno($ch);
            unset($ch);
            if ($failed || $status < 200 || $status >= 300 || !is_string($result)) { return ''; }
            if ($output_type === 'text') { return trim($result); }
            $response = json_decode($result, true, 512, JSON_THROW_ON_ERROR);
            $segments = [];
            foreach ($response['segments'] ?? [] as $segment) {
                if (!is_string($segment['text'] ?? null) || !is_numeric($segment['start'] ?? null) || !is_numeric($segment['end'] ?? null)) { return ''; }
                $segments[] = ['speaker'=>'0', 'start'=>(float)$segment['start'], 'end'=>(float)$segment['end'], 'text'=>trim($segment['text'])];
            }
            return $segments ? json_encode(['segments'=>$segments], JSON_THROW_ON_ERROR) : '';
        } catch (Throwable $e) {
            return ''; // Preserve normal voicemail processing; do not log audio or credentials.
        } finally {
            if (is_resource($lock)) { flock($lock, LOCK_UN); fclose($lock); }
        }
    }
}
