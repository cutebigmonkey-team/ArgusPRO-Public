<?php
// Offline entry point: these upstream methods receive one guarded HTTP response.
// No FreshRSS server, RSS-Bridge endpoint, account session or second HTTP fetch.
$job = json_decode(file_get_contents($argv[1]), true, 512, JSON_THROW_ON_ERROR);
$html = file_get_contents($job['html']);
if (strlen($html) > 4*1024*1024) throw new Exception('response_too_large');
if ($job['tool'] === 'instagramprofiles') {
    require $job['tools'].'/instagramprofiles/lib/InstagramProfilesPage.php';
    $result = ['profile'=>InstagramProfilesPage::parseEmbed($html)];
} elseif ($job['tool'] === 'rss-bridge') {
    $root = $job['tools'].'/rss-bridge';
    require $root.'/lib/utils.php';
    require $root.'/lib/simplehtmldom/simple_html_dom.php';
    require $root.'/lib/BridgeAbstract.php';
    require $root.'/bridges/FacebookBridge.php';
    $bridge = (new ReflectionClass(FacebookBridge::class))->newInstanceWithoutConstructor();
    $dom = new simple_html_dom();
    $dom->load($html);
    $extract = new ReflectionMethod(FacebookBridge::class, 'extractTimelineNodes');
    $parse = new ReflectionMethod(FacebookBridge::class, 'parseTimelineNode');
    $nodes = $extract->invoke($bridge, $dom);
    $rows = [];
    foreach (array_slice($nodes, 0, 20) as $node) {
        $item = $parse->invoke($bridge, $node, ''); // Never use a profile avatar as post media.
        $item['actors'] = $node['comet_sections']['content']['story']['actors'] ?? [];
        $rows[] = $item;
    }
    $canonical = $dom->find('meta[property="og:url"]',0);
    $result = ['items'=>$rows,'canonical'=>$canonical ? $canonical->content : ''];
} else {
    throw new Exception('unsupported_tool');
}
file_put_contents($argv[2],json_encode($result,JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR));
