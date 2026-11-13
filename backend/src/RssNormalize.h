#pragma once

#include <string>
#include <vector>

namespace argus::rss {

struct Item {
    std::string title, link, published;
};

std::string unescape(std::string value);
std::string tag(const std::string &block, const std::string &name);
std::string atomLink(const std::string &block);
std::string canonicalize(const std::string &url);
std::string lower(std::string value);
std::string resolveUrl(const std::string &base, const std::string &reference);
bool isFeed(const std::string &body);
std::vector<Item> parse(const std::string &xml, size_t limit);

}  // namespace argus::rss
