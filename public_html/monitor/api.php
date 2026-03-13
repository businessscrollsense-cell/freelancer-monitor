<?php
/**
 * Freelancer Monitor — Settings API
 * GET  /monitor/api.php          → returns current settings + status JSON
 * POST /monitor/api.php          → saves new settings, returns success/error JSON
 *
 * BASE_DIR points to your home folder where the Python scripts live.
 * Standard cPanel layout:  /home/username/public_html/monitor/  (this file)
 *                           /home/username/                       (BASE_DIR)
 */

define('BASE_DIR', dirname(dirname(__DIR__)));   // two levels up from this file

$SETTINGS_FILE = BASE_DIR . '/settings.json';
$LAST_RUN_FILE = BASE_DIR . '/last_run.json';
$RECENT_FILE   = BASE_DIR . '/recent_alerts.json';

header('Content-Type: application/json');
header('Access-Control-Allow-Origin: same-origin');

// ── Helpers ─────────────────────────────────────────────────────────────────

function readJSON($path, $default = []) {
    if (!file_exists($path)) return $default;
    $data = json_decode(file_get_contents($path), true);
    return ($data !== null) ? $data : $default;
}

function respond($data, $code = 200) {
    http_response_code($code);
    echo json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE);
    exit;
}

// ── GET — return current state ───────────────────────────────────────────────

if ($_SERVER['REQUEST_METHOD'] === 'GET') {
    $settings = readJSON($SETTINGS_FILE);
    $lastRun  = readJSON($LAST_RUN_FILE);
    $recent   = readJSON($RECENT_FILE, []);

    // Strip secrets from the response sent to the browser
    $publicSettings = $settings;
    unset($publicSettings['freelancer_token'], $publicSettings['telegram_bot_token']);

    respond([
        'ok'       => true,
        'settings' => $publicSettings,
        'last_run' => $lastRun,
        'recent'   => $recent,
    ]);
}

// ── POST — save settings ─────────────────────────────────────────────────────

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $body = file_get_contents('php://input');
    $posted = json_decode($body, true);

    if (!is_array($posted)) {
        respond(['ok' => false, 'error' => 'Invalid JSON body'], 400);
    }

    // Load existing settings so we never overwrite secrets via the web form
    $current = readJSON($SETTINGS_FILE);

    // Only allow updating these fields from the web form
    $allowed = ['skills', 'countries', 'min_fixed_budget', 'min_hourly_budget', 'lookback_minutes'];
    foreach ($allowed as $key) {
        if (array_key_exists($key, $posted)) {
            $current[$key] = $posted[$key];
        }
    }

    // Validate & sanitise
    if (isset($current['skills']) && !is_array($current['skills'])) {
        respond(['ok' => false, 'error' => "'skills' must be an array"], 400);
    }
    if (isset($current['countries']) && !is_array($current['countries'])) {
        respond(['ok' => false, 'error' => "'countries' must be an array"], 400);
    }
    if (isset($current['min_fixed_budget'])) {
        $current['min_fixed_budget'] = max(0, (int)$current['min_fixed_budget']);
    }

    // Filter out empty strings
    if (isset($current['skills'])) {
        $current['skills'] = array_values(array_filter(array_map('trim', $current['skills'])));
    }
    if (isset($current['countries'])) {
        $current['countries'] = array_values(array_filter(array_map('trim', $current['countries'])));
    }

    // Write atomically
    $tmp = $SETTINGS_FILE . '.tmp';
    $written = file_put_contents($tmp, json_encode($current, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));
    if ($written === false) {
        respond(['ok' => false, 'error' => 'Could not write settings file. Check folder permissions.'], 500);
    }
    rename($tmp, $SETTINGS_FILE);

    respond(['ok' => true, 'message' => 'Settings saved successfully.']);
}

respond(['ok' => false, 'error' => 'Method not allowed'], 405);
