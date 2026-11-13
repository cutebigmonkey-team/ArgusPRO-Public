<?php
// No public endpoint, token-bearing URL, or upstream exception is emitted.
if (getenv('GITHUB_ACTIONS') !== 'true' || getenv('RUNNER_ENVIRONMENT') !== 'github-hosted') exit(2);
$output = $argv[3];
$limit = isset($argv[4]) ? (int)$argv[4] : 5;
if ($limit < 1 || $limit > 100) exit(2);
$token = getenv('ARGUS_VK_TOKEN');
if (!$token) { file_put_contents($output, json_encode(['status'=>'requires_vk_token'])); exit(0); }
chdir($argv[1]);
require_once 'Vk2rss.php';
class ArgusVkRss extends Vk2rss {
    private $requests = 0;
    protected function getContent($api_method, $offset = null, $donut = false, $params = array('extended'=>1, 'fields'=>'first_name_ins,last_name_ins,first_name_gen,last_name_gen,screen_name')) {
        if (++$this->requests > 3 || !in_array($api_method, ['wall.get', 'video.get'], true) || $donut) throw new Exception('request_budget');
        $params['access_token'] = $this->access_token;
        $params['v'] = '5.199';
        global $limit;
        $params['count'] = $limit;
        if ($api_method === 'wall.get') {
            $params['filter'] = 'owner';
            if ($offset !== null) {
                if (filter_var($offset, FILTER_VALIDATE_INT) === false || (int)$offset < 0) throw new Exception('invalid_offset');
                $params['offset'] = (int)$offset;
            }
            if ($this->domain) $params['domain'] = $this->domain;
            else $params['owner_id'] = $this->owner_id;
        }
        $curl = curl_init('https://api.vk.com/method/'.$api_method);
        $body = '';
        curl_setopt_array($curl, [CURLOPT_POST=>true, CURLOPT_POSTFIELDS=>http_build_query($params),
            CURLOPT_FOLLOWLOCATION=>false, CURLOPT_CONNECTTIMEOUT=>8, CURLOPT_TIMEOUT=>12,
            CURLOPT_WRITEFUNCTION=>function($ch, $chunk) use (&$body) {
                if (strlen($body)+strlen($chunk)>4*1024*1024) return 0;
                $body .= $chunk; return strlen($chunk);
            }]);
        $ok = curl_exec($curl); $status = curl_getinfo($curl, CURLINFO_HTTP_CODE); curl_close($curl);
        if (!$ok || $status !== 200) throw new Exception(in_array($status,[401,403,429]) ? 'http_'.$status : 'network_error');
        $data = json_decode($body);
        if (!$data) throw new Exception('invalid_response');
        if (isset($data->error)) {
            $code = $data->error->error_code;
            throw new Exception($code == 6 ? 'http_429' : ($code == 5 ? 'invalid_vk_token' : 'vk_api_error'));
        }
        return $data;
    }
}
ob_start();
try {
    $feed = new ArgusVkRss(['id'=>$argv[2], 'access_token'=>$token, 'count'=>$limit, 'owner_only'=>true,
                          'skip_ads'=>true, 'donut'=>false, 'allow_embedded_video'=>false]);
    $feed->generateRSS();
    $rss = ob_get_clean();
    if (strpos($rss, $token) !== false) throw new Exception('invalid_output');
    $result = ['status'=>'ok', 'rss'=>$rss];
} catch (Throwable $e) {
    if (ob_get_level()) ob_end_clean();
    $allowed = ['http_401','http_403','http_429','network_error','invalid_vk_token','vk_api_error','request_budget'];
    $result = ['status'=>in_array($e->getMessage(),$allowed,true) ? $e->getMessage() : 'extraction_failed'];
}
file_put_contents($output, json_encode($result));
