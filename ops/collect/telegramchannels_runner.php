<?php
declare(strict_types=1);
// Offline conversion of the already fetched Telegram page; no FreshRSS server.
require $argv[1] . '/lib/TelegramChannelsPage.php';
$request = json_decode(stream_get_contents(STDIN), true, flags: JSON_THROW_ON_ERROR);
$data = TelegramChannelsPage::parse($request['html'], $request['channel']);
echo json_encode($data, JSON_THROW_ON_ERROR | JSON_INVALID_UTF8_SUBSTITUTE);
