#include "PlatformAdapter.h"

#include <iostream>
#include <string>

namespace {
int failures = 0;
void expect(bool condition, const char *message)
{
    if (!condition) { std::cerr << "FAIL: " << message << '\n'; ++failures; }
}
}

int main()
{
    using namespace argus::browser;
    struct Case { const char *platform; const char *url; const char *key; };
    const Case cases[] = {
        {"facebook", "https://www.facebook.com/a/posts/12345?ref=x", "facebook:12345"},
        {"instagram", "https://www.instagram.com/p/ABC_12/?utm_source=x", "instagram:ABC_12"},
        {"x", "https://twitter.com/account/status/123456?ref_src=x", "x:123456"},
        {"threads", "https://www.threads.net/@account/post/AbC123", "threads:AbC123"},
        {"tiktok", "https://www.tiktok.com/@account/video/74500123", "tiktok:74500123"},
        {"vk", "https://vk.com/wall-99_7?from=feed", "vk:-99:7"},
        {"ok", "https://ok.ru/group/1/topic/987654", "ok:987654"},
        {"max", "https://web.max.ru/c/-12345/AZ2qkK1xdkU?ref=x", "max:-12345:AZ2qkK1xdkU"},
        {"dsns-browser", "https://fixture.invalid/source/43news/item?x=1", "dsns-browser:https://fixture.invalid/source/43news/item"},
    };
    for (const auto &test : cases)
    {
        auto adapter = makePlatformAdapter(test.platform);
        expect(adapter != nullptr, "adapter factory");
        const auto [key, url] = adapter->spec().canonicalizer(test.url);
        expect(key == test.key, "canonical key");
        expect(!url.empty(), "canonical URL");
        const std::string &expression = adapter->spec().extractionExpression;
        const std::size_t invocation = expression.rfind("()");
        std::size_t closingParentheses = 0;
        if (invocation != std::string::npos)
            for (std::size_t at = invocation; at > 0 && expression[at - 1] == ')'; --at)
                ++closingParentheses;
        expect(invocation + 2 == expression.size() && closingParentheses >= 1 &&
                   closingParentheses <= 2,
               "extraction expression is a balanced IIFE");
    }

    auto vk = makeVkAdapter();
    expect(vk->spec().extractionExpression.find(".post_info") == std::string::npos &&
               vk->spec().extractionExpression.find("el.innerText") == std::string::npos,
           "VK extraction excludes localized interface chrome");
    Json::Value vkRows(Json::arrayValue);
    Json::Value russianVkPost;
    russianVkPost["url"] = "https://vk.com/wall-99_8";
    russianVkPost["text"] = "Оригинальный текст публикации";
    vkRows.append(russianVkPost);
    const auto parsedVk = vk->parseDom(vkRows, 20);
    expect(parsedVk.size() == 1 &&
               parsedVk.front().text == "Оригинальный текст публикации",
           "VK adapter preserves the authored source language");

    auto facebook = makeFacebookAdapter();
    expect(facebook->spec().fallbackThreshold == 2 && facebook->needsFallback(0) &&
               facebook->needsFallback(1) && !facebook->needsFallback(2),
           "Facebook mbasic fallback is adaptive below two verified posts");
    expect(facebook->fallbackNavigationUrl("https://www.facebook.com/agency?locale=uk_UA") ==
               "https://mbasic.facebook.com/agency?locale=uk_UA",
           "Facebook mbasic fallback preserves the verified source path");
    expect(facebook->fallbackNavigationUrl("https://www.facebook.com/share/g/invite").empty(),
           "Facebook share invites cannot become recurring fallback sources");

    const auto [facebookReelKey, facebookReelUrl] = facebook->spec().canonicalizer(
        "https://www.facebook.com/reel/987654321?tracking=x");
    expect(facebookReelKey == "facebook:987654321" &&
               facebookReelUrl == "https://www.facebook.com/reel/987654321",
           "Facebook reel canonical identity is supported");
    const auto [facebookVideoKey, facebookVideoUrl] = facebook->spec().canonicalizer(
        "https://www.facebook.com/agency/videos/123456789/?tracking=x");
    expect(facebookVideoKey == "facebook:123456789" &&
               facebookVideoUrl == "https://www.facebook.com/agency/videos/123456789/",
           "Facebook video canonical identity is supported");
    expect(facebook->spec().canonicalizer(
               "https://www.facebook.com/agency/posts/123?comment_id=456").first.empty(),
           "Facebook comment permalink cannot become a post");
    expect(facebook->spec().canonicalizer(
               "https://www.facebook.com/story.php?story_fbid=Y29tbWVudDoxMjM=&id=99").first.empty(),
           "Facebook base64 comment identity cannot become a post");
    expect(facebook->spec().canonicalizer(
               "https://www.facebook.com/story.php?story_fbid=comment_id:123&id=99").first.empty(),
           "Facebook named comment identity cannot become a post");

    Json::Value facebookDom(Json::arrayValue);
    Json::Value ownedFacebookPost;
    ownedFacebookPost["url"] = "https://www.facebook.com/agency/posts/100";
    ownedFacebookPost["text"] = "owned post";
    ownedFacebookPost["publishedAt"] = "2026-07-25T20:00:00Z";
    facebookDom.append(ownedFacebookPost);
    Json::Value foreignFacebookPost = ownedFacebookPost;
    foreignFacebookPost["url"] = "https://www.facebook.com/another/posts/101";
    facebookDom.append(foreignFacebookPost);
    Json::Value ownedFacebookReel = ownedFacebookPost;
    ownedFacebookReel["url"] = "https://www.facebook.com/reel/102";
    ownedFacebookReel["ownerUrl"] = "https://mbasic.facebook.com/agency";
    facebookDom.append(ownedFacebookReel);
    Json::Value foreignFacebookReel = ownedFacebookReel;
    foreignFacebookReel["url"] = "https://www.facebook.com/reel/103";
    foreignFacebookReel["ownerUrl"] = "https://mbasic.facebook.com/another";
    facebookDom.append(foreignFacebookReel);
    const auto verifiedFacebookDom = facebook->parseDomForSource(
        facebookDom, "https://www.facebook.com/agency", 20);
    expect(verifiedFacebookDom.size() == 2 &&
               verifiedFacebookDom.front().canonicalKey == "facebook:100" &&
               verifiedFacebookDom.back().canonicalKey == "facebook:102",
           "Facebook DOM accepts only source-verifiable posts and reels");

    Json::Value mbasicRows(Json::arrayValue);
    Json::Value mbasicOwned;
    mbasicOwned["url"] =
        "https://mbasic.facebook.com/story.php?story_fbid=200&id=99";
    mbasicOwned["ownerUrl"] = "https://mbasic.facebook.com/agency";
    mbasicOwned["ownerId"] = "99";
    mbasicOwned["text"] = "mbasic owned story";
    mbasicOwned["publishedAt"] = "1753460000";
    mbasicRows.append(mbasicOwned);
    Json::Value mbasicForeign = mbasicOwned;
    mbasicForeign["url"] =
        "https://mbasic.facebook.com/story.php?story_fbid=201&id=88";
    mbasicForeign["ownerUrl"] = "https://mbasic.facebook.com/another";
    mbasicForeign["ownerId"] = "88";
    mbasicRows.append(mbasicForeign);
    expect(facebook->parseFallbackDom(
               mbasicRows, "https://www.facebook.com/agency", 20).size() == 1,
           "Facebook mbasic fallback rejects stories from another source");
    expect(facebook->parseFallbackDom(
               mbasicRows, "https://www.facebook.com/profile.php?id=99", 20).size() == 1,
           "Facebook mbasic fallback verifies numeric page ownership");

    Json::Value facebookNetworkResponse;
    Json::Value facebookNetworkPost;
    facebookNetworkPost["__typename"] = "Story";
    facebookNetworkPost["post_id"] = "300";
    facebookNetworkPost["creation_time"] = 1753460100;
    facebookNetworkPost["permalink_url"] = "https://www.facebook.com/agency/posts/300";
    facebookNetworkPost["message"]["text"] = "verified network story";
    facebookNetworkResponse["data"]["nodes"].append(facebookNetworkPost);
    Json::Value unrelatedNetworkPost = facebookNetworkPost;
    unrelatedNetworkPost["post_id"] = "301";
    unrelatedNetworkPost["permalink_url"] = "https://www.facebook.com/another/posts/301";
    facebookNetworkResponse["data"]["nodes"].append(unrelatedNetworkPost);
    Json::Value commentNode = facebookNetworkPost;
    commentNode["__typename"] = "Comment";
    commentNode["post_id"] = "302";
    commentNode["permalink_url"] =
        "https://www.facebook.com/agency/posts/300?comment_id=302";
    commentNode["nested"] = facebookNetworkPost;
    facebookNetworkResponse["data"]["nodes"].append(commentNode);
    Json::Value encodedComment = facebookNetworkPost;
    encodedComment["post_id"] = "Y29tbWVudDoxMjM=";
    encodedComment["permalink_url"] =
        "https://www.facebook.com/story.php?story_fbid=Y29tbWVudDoxMjM=&id=99";
    encodedComment["ownerUrl"] = "https://www.facebook.com/agency";
    facebookNetworkResponse["data"]["nodes"].append(encodedComment);
    Json::Value explicitCommentId = facebookNetworkPost;
    explicitCommentId["post_id"] = "303";
    explicitCommentId["comment_id"] = "456";
    explicitCommentId["permalink_url"] = "https://www.facebook.com/agency/posts/303";
    facebookNetworkResponse["data"]["nodes"].append(explicitCommentId);
    const auto verifiedFacebookNetwork = facebook->parseNetwork(
        {facebookNetworkResponse}, "https://www.facebook.com/agency", 20);
    expect(verifiedFacebookNetwork.size() == 1 &&
               verifiedFacebookNetwork.front().canonicalKey == "facebook:300",
           "Facebook network parsing rejects comments and unrelated graph nodes");

    auto x = makeXAdapter();
    Json::Value raw(Json::arrayValue);
    Json::Value row;
    row["url"] = "https://x.com/a/status/1";
    row["text"] = "post";
    row["images"] = Json::Value(Json::arrayValue);
    row["videos"] = Json::Value(Json::arrayValue);
    raw.append(row);
    raw.append(row);
    expect(x->parseDom(raw, 10).size() == 1, "duplicate replay suppressed by canonical key");
    expect(x->classifyPage("https://x.com/login", "Sign in to X") == "auth_required",
           "auth classification");
    expect(x->classifyPage("https://x.com/a", "Verify you are human captcha") == "challenge_required",
           "challenge classification");

    auto threads = makeThreadsAdapter();
    expect(threads->navigationUrl("https://threads.net/@dsns_ukraine/?hl=uk") ==
               "https://www.threads.com/@dsns_ukraine",
           "Threads recurring source navigation is normalized");
    expect(threads->navigationUrl(
               "https://www.threads.com/@dsns_ukraine/post/ABC123").empty(),
           "Threads post URL cannot be configured as a recurring profile source");
    expect(threads->spec().canonicalizer(
               "https://www.threads.com/@dsns_ukraine/post/ABC123/more").first.empty(),
           "Threads malformed post suffix cannot fabricate a post identity");

    Json::Value threadsDom(Json::arrayValue);
    Json::Value ownedThreadsPost;
    ownedThreadsPost["url"] =
        "https://www.threads.com/@dsns_ukraine/post/ABC123?tracking=x";
    ownedThreadsPost["text"] = "ДСНС post";
    ownedThreadsPost["publishedAt"] = "2026-08-05T10:00:00Z";
    threadsDom.append(ownedThreadsPost);
    Json::Value suggestedThreadsPost = ownedThreadsPost;
    suggestedThreadsPost["url"] =
        "https://www.threads.com/@foreign_account/post/FOREIGN1";
    threadsDom.append(suggestedThreadsPost);
    const auto verifiedThreadsDom = threads->parseDomForSource(
        threadsDom, "https://www.threads.com/@dsns_ukraine", 20);
    expect(verifiedThreadsDom.size() == 1 &&
               verifiedThreadsDom.front().canonicalKey == "threads:ABC123",
           "Threads DOM rejects suggested posts from another profile");

    Json::Value threadsResponse;
    Json::Value ownedThreadsNetworkPost;
    ownedThreadsNetworkPost["code"] = "NETWORK1";
    ownedThreadsNetworkPost["taken_at"] = 1785924000;
    ownedThreadsNetworkPost["caption"]["text"] = "ДСНС network post";
    ownedThreadsNetworkPost["user"]["username"] = "dsns_ukraine";
    threadsResponse["data"]["items"].append(ownedThreadsNetworkPost);
    Json::Value foreignThreadsNetworkPost = ownedThreadsNetworkPost;
    foreignThreadsNetworkPost["code"] = "NETWORK2";
    foreignThreadsNetworkPost["user"]["username"] = "foreign_account";
    threadsResponse["data"]["items"].append(foreignThreadsNetworkPost);
    const auto verifiedThreadsNetwork = threads->parseNetwork(
        {threadsResponse}, "https://www.threads.com/@dsns_ukraine", 20);
    expect(verifiedThreadsNetwork.size() == 1 &&
               verifiedThreadsNetwork.front().canonicalKey == "threads:NETWORK1" &&
               verifiedThreadsNetwork.front().postUrl ==
                   "https://www.threads.com/@dsns_ukraine/post/NETWORK1",
           "Threads network parsing keeps only posts owned by the configured profile");

    expect(threads->classifyPage("https://www.threads.com/@account", "Log in to Threads").empty(),
           "public Threads login CTA is ignored before extraction");
    expect(threads->classifyEmptyPage("https://www.threads.com/@account", "Log in to Threads") ==
               "auth_required",
           "empty Threads login wall classification");
    expect(threads->classifyEmptyPage("https://www.threads.com/@account", "Verify you are human") ==
               "challenge_required",
           "empty Threads challenge classification");
    expect(threads->classifyEmptyPage("https://www.threads.com/@account", " \n\t") ==
               "empty_shell",
           "empty Threads shell classification");
    expect(threads->classifyEmptyPage("https://www.threads.com/@account", "Profile unavailable").empty(),
           "unknown non-empty Threads response remains selector regression");

    auto tiktok = makeTikTokAdapter();
    expect(tiktok->spec().scrollSteps == 3, "tiktok challenge exposure keeps scrolling bounded");
    expect(tiktok->spec().extractionExpression.rfind("(()=>{try{", 0) == 0 &&
               tiktok->spec().extractionExpression.find("catch{return []}") != std::string::npos,
           "tiktok extraction contains the whole IIFE exception boundary");
    expect(tiktok->navigationUrl("https://tiktok.com/@radio.nv/?lang=uk") ==
               "https://www.tiktok.com/@radio.nv",
           "tiktok recurring source navigation is normalized");
    expect(tiktok->navigationUrl("https://vt.tiktok.com/Z-short/").empty(),
           "tiktok short video link is rejected as a recurring source");
    expect(tiktok->navigationUrl("https://www.tiktok.com/@radio.nv?next=/video/74500123") ==
               "https://www.tiktok.com/@radio.nv",
           "tiktok query text cannot be mistaken for a post path");
    expect(tiktok->spec().canonicalizer(
               "https://www.tiktok.com/@radio.nv?next=/video/74500123").first.empty(),
           "tiktok profile query cannot fabricate a post identity");
    expect(tiktok->spec().canonicalizer(
               "https://www.tiktok.com/@radio.nv/video/123junk").first.empty(),
           "tiktok malformed video suffix cannot fabricate a post identity");
    expect(tiktok->classifyPage("https://www.tiktok.com/@account", "Please wait...").empty(),
           "short TikTok SPA shell is not rejected before extraction");
    expect(tiktok->classifyEmptyPage("https://www.tiktok.com/@account",
                                     "Please wait...") == "challenge_required",
           "empty TikTok WAF shell classification");
    Json::Value malformedDom(Json::arrayValue);
    Json::Value malformedDomRow;
    malformedDomRow["url"] = "https://www.tiktok.com/@radio.nv/video/74500123/junk";
    malformedDomRow["text"] = "not a post permalink";
    malformedDom.append(malformedDomRow);
    expect(tiktok->parseDom(malformedDom, 20).empty(),
           "TikTok DOM row cannot sanitize a non-post subpath into a post");
    Json::Value tiktokResponse;
    Json::Value matchingItem;
    matchingItem["id"] = "74500123001";
    matchingItem["createTime"] = 1785010000;
    matchingItem["desc"] = "matching";
    matchingItem["author"]["uniqueId"] = "radio.nv";
    tiktokResponse["itemList"].append(matchingItem);
    Json::Value foreignItem = matchingItem;
    foreignItem["id"] = "74500123002";
    foreignItem["author"]["uniqueId"] = "another.profile";
    tiktokResponse["itemList"].append(foreignItem);
    Json::Value stringAuthorItem = matchingItem;
    stringAuthorItem["id"] = "74500123003";
    stringAuthorItem["author"] = "radio.nv";
    tiktokResponse["itemList"].append(stringAuthorItem);
    const auto tiktokNetwork = tiktok->parseNetwork(
        {tiktokResponse}, "https://www.tiktok.com/@radio.nv?lang=uk", 20);
    expect(tiktokNetwork.size() == 2 &&
               tiktokNetwork.front().canonicalKey == "tiktok:74500123001" &&
               tiktokNetwork.back().canonicalKey == "tiktok:74500123003",
           "tiktok network fallback accepts exact string/object authors and rejects foreign authors");

    auto max = makeMaxAdapter();
    expect(max->navigationUrl("https://max.ru/channel_example?ref=x") ==
               "https://web.max.ru/channel_example",
           "MAX landing navigates to the web client");
    expect(max->navigationUrl("https://web.max.ru/channel_example") ==
               "https://web.max.ru/channel_example",
           "MAX web-client URL remains stable");
    expect(max->navigationUrl("https://www.max.ru/c/-12345/AZ2qkK1xdkU?ref=x") ==
               "https://web.max.ru/c/-12345/AZ2qkK1xdkU",
           "MAX post navigation preserves the verified route");
    expect(max->navigationUrl("https://max.ru/join/private-token").empty() &&
               max->navigationUrl("https://max.ru/").empty(),
           "MAX unsafe or non-channel routes are rejected");
    const auto [maxKey, maxUrl] = max->spec().canonicalizer(
        "https://www.max.ru/c/-12345/AZ2qkK1xdkU?tracking=x");
    expect(maxKey == "max:-12345:AZ2qkK1xdkU" &&
               maxUrl == "https://max.ru/c/-12345/AZ2qkK1xdkU",
           "MAX post identity is host-independent and normalized");
    expect(max->spec().canonicalizer("https://max.ru/channel/post/24680").first.empty(),
           "unverified legacy MAX post shape is rejected");
    expect(max->spec().canonicalizer("https://max.ru/other/c/-12345/AZ2qkK1xdkU").first.empty(),
           "MAX post route must start at the host root");

    if (failures == 0) std::cout << "browser platform adapter tests passed\n";
    return failures == 0 ? 0 : 1;
}
