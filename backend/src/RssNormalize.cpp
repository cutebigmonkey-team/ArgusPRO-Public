#include "RssNormalize.h"

#include <curl/curl.h>

#include <algorithm>
#include <cctype>

namespace argus::rss {

std::string unescape(std::string value)
{
    const std::pair<const char *, const char *> replacements[] = {
        {"&amp;", "&"}, {"&lt;", "<"}, {"&gt;", ">"}, {"&quot;", "\""}, {"&#39;", "'"}};
    for (const auto &[from, to] : replacements)
        for (size_t at = 0; (at = value.find(from, at)) != std::string::npos; at += std::char_traits<char>::length(to))
            value.replace(at, std::char_traits<char>::length(from), to);
    return value;
}

std::string tag(const std::string &block, const std::string &name)
{
    const std::string open = "<" + name;
    auto begin = block.find(open);
    if (begin == std::string::npos) return {};
    begin = block.find('>', begin);
    if (begin == std::string::npos) return {};
    const auto end = block.find("</" + name + ">", ++begin);
    if (end == std::string::npos) return {};
    std::string value = block.substr(begin, end - begin);
    if (value.rfind("<![CDATA[", 0) == 0 && value.size() >= 12) value = value.substr(9, value.size() - 12);
    return unescape(value);
}

std::string atomLink(const std::string &block)
{
    const auto begin = block.find("<link");
    if (begin == std::string::npos) return {};
    const auto end = block.find('>', begin);
    if (end == std::string::npos) return {};
    const std::string element = block.substr(begin, end - begin);
    auto href = element.find("href=");
    if (href == std::string::npos) return {};
    href += 5;
    if (href >= element.size() || (element[href] != '\'' && element[href] != '\"')) return {};
    const char quote = element[href++];
    const auto close = element.find(quote, href);
    return close == std::string::npos ? std::string{} : unescape(element.substr(href, close - href));
}

std::string canonicalize(const std::string &url)
{
    CURLU *parsed = curl_url();
    if (!parsed) return url;
    if (curl_url_set(parsed, CURLUPART_URL, url.c_str(), 0) != CURLUE_OK)
    {
        curl_url_cleanup(parsed);
        return url;
    }
    curl_url_set(parsed, CURLUPART_FRAGMENT, nullptr, 0);
    char *normalized = nullptr;
    const CURLUcode result = curl_url_get(parsed, CURLUPART_URL, &normalized, 0);
    std::string output = result == CURLUE_OK && normalized != nullptr ? normalized : url;
    curl_free(normalized);
    curl_url_cleanup(parsed);
    return output;
}

std::string lower(std::string value)
{
    std::transform(value.begin(), value.end(), value.begin(),
                   [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); });
    return value;
}

std::string resolveUrl(const std::string &base, const std::string &reference)
{
    if (reference.empty()) return {};
    CURLU *parsed = curl_url();
    if (!parsed) return reference;
    if (curl_url_set(parsed, CURLUPART_URL, base.c_str(), 0) != CURLUE_OK ||
        curl_url_set(parsed, CURLUPART_URL, reference.c_str(), 0) != CURLUE_OK)
    {
        curl_url_cleanup(parsed);
        return reference;
    }
    char *resolved = nullptr;
    const CURLUcode result = curl_url_get(parsed, CURLUPART_URL, &resolved, 0);
    std::string output = result == CURLUE_OK && resolved != nullptr ? resolved : reference;
    curl_free(resolved);
    curl_url_cleanup(parsed);
    return output;
}

bool isFeed(const std::string &body)
{
    const std::string head = lower(body.substr(0, std::min<size_t>(body.size(), 4096)));
    return head.find("<rss") != std::string::npos || head.find("<feed") != std::string::npos ||
           head.find("<rdf:rdf") != std::string::npos || head.find("<item") != std::string::npos ||
           head.find("<entry") != std::string::npos;
}

std::vector<Item> parse(const std::string &xml, size_t limit)
{
    std::vector<Item> items;
    size_t cursor = 0;
    while (items.size() < limit)
    {
        auto begin = xml.find("<item", cursor);
        std::string close = "</item>";
        if (begin == std::string::npos) { begin = xml.find("<entry", cursor); close = "</entry>"; }
        if (begin == std::string::npos) break;
        const auto end = xml.find(close, begin);
        if (end == std::string::npos) break;
        const std::string block = xml.substr(begin, end - begin);
        Item item{tag(block, "title"), tag(block, "link"), tag(block, "pubDate")};
        if (item.link.empty()) item.link = atomLink(block);
        if (item.link.empty()) item.link = tag(block, "id");
        if (item.published.empty()) item.published = tag(block, "published");
        if (!item.link.empty()) items.push_back(std::move(item));
        cursor = end + close.size();
    }
    return items;
}

}  // namespace argus::rss
