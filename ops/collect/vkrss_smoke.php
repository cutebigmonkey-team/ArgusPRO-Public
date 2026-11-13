<?php
// Actual upstream RSS generator with an offline API fixture; no credentials.
chdir($argv[1]);
require_once 'Vk2rss.php';
class FixtureVkRss extends Vk2rss {
    protected function getContent($api_method, $offset = null, $donut = false, $params = array('extended'=>1)) {
        if ($api_method !== 'wall.get') throw new Exception('unexpected API request');
        return json_decode('{"response":{"count":1,"items":[{"id":42,"owner_id":-7,"from_id":-7,"date":1793318400,"text":"Український допис","marked_as_ads":0,"comments":{"count":0},"attachments":[]}],"profiles":[],"groups":[{"id":7,"name":"Example","screen_name":"example"}]}}');
    }
}
$feed = new FixtureVkRss(['id'=>'club7','access_token'=>'offline-fixture','count'=>5,
                         'owner_only'=>true,'skip_ads'=>true,'donut'=>false,'allow_embedded_video'=>false]);
$feed->generateRSS();
