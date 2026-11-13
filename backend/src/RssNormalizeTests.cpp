// Read-only diagnostic: exercises argus::rss canonicalization against the
// URL edge cases required by the RSS shadow-validation evidence table
// (docs/rss-shadow-validation.md, section "Canonical URL"). Not linked into
// any runtime collector; failures here are validation findings, not crashes.
#include "RssNormalize.h"

#include <iostream>
#include <string>

namespace {
int failures = 0;

void check(const std::string &name, const std::string &actual, const std::string &expected)
{
    if (actual != expected)
    {
        ++failures;
        std::cout << "FAIL " << name << "\n  expected: " << expected << "\n  actual:   " << actual << "\n";
    }
    else
    {
        std::cout << "ok   " << name << "\n";
    }
}

void checkTrue(const std::string &name, bool condition, const std::string &detail)
{
    if (!condition)
    {
        ++failures;
        std::cout << "FAIL " << name << "  (" << detail << ")\n";
    }
    else
    {
        std::cout << "ok   " << name << "\n";
    }
}
}  // namespace

int main()
{
    using namespace argus::rss;

    // --- fragment stripping (already handled) ---
    check("fragment_removed",
          canonicalize("https://example.com/a?x=1#section2"),
          "https://example.com/a?x=1");

    // --- http vs https: current behavior does NOT unify scheme ---
    {
        const auto http = canonicalize("http://example.com/a");
        const auto https = canonicalize("https://example.com/a");
        checkTrue("http_https_share_canonical_id (KNOWN GAP)", http == https,
                  "http and https variants of the same article currently produce different canonical_key values");
    }

    // --- trailing slash: current behavior does NOT unify ---
    {
        const auto bare = canonicalize("https://example.com/a");
        const auto slash = canonicalize("https://example.com/a/");
        checkTrue("trailing_slash_unified (KNOWN GAP)", bare == slash,
                  "'/a' and '/a/' currently produce different canonical_key values");
    }

    // --- utm_* / tracking query params: current behavior does NOT strip ---
    {
        const auto clean = canonicalize("https://example.com/a?id=5");
        const auto tracked = canonicalize("https://example.com/a?id=5&utm_source=telegram&utm_medium=social");
        checkTrue("utm_params_stripped (KNOWN GAP)", clean == tracked,
                  "utm_* query parameters currently survive canonicalization and change the canonical_key");
    }

    // --- percent-encoding: libcurl's URL parser should normalize case of escapes ---
    check("percent_encoding_case",
          canonicalize("https://example.com/%2Fa%2Fb"),
          canonicalize("https://example.com/%2fa%2fb"));

    // --- HTML entities in <link> text must be unescaped before use as a URL ---
    check("html_entity_ampersand",
          unescape("https://example.com/a?x=1&amp;y=2"),
          "https://example.com/a?x=1&y=2");

    // --- relative URL resolution against a feed's own address ---
    check("relative_path_resolved",
          resolveUrl("https://example.com/rss/", "/news/article-1"),
          "https://example.com/news/article-1");
    check("relative_dotdot_resolved",
          resolveUrl("https://example.com/rss/current/", "../archive/x"),
          "https://example.com/rss/archive/x");
    check("protocol_relative_resolved",
          resolveUrl("https://example.com/rss/", "//cdn.example.com/x"),
          "https://cdn.example.com/x");

    // --- duplicate links: two <item> blocks pointing at the same article ---
    {
        const std::string xml =
            "<rss><channel>"
            "<item><title>A</title><link>https://example.com/a</link><pubDate>Mon</pubDate></item>"
            "<item><title>A (mirror)</title><link>https://example.com/a</link><pubDate>Mon</pubDate></item>"
            "</channel></rss>";
        const auto items = parse(xml, 10);
        checkTrue("parser_yields_two_raw_items", items.size() == 2, "parse() does not itself dedupe");
        if (items.size() == 2)
        {
            const auto keyA = "rss:" + canonicalize(resolveUrl("https://example.com/", items[0].link));
            const auto keyB = "rss:" + canonicalize(resolveUrl("https://example.com/", items[1].link));
            check("duplicate_items_share_canonical_key", keyA, keyB);
        }
    }

    // --- Atom <entry> with <link href="..."/> instead of RSS <link> text ---
    {
        const std::string entry = "<entry><link rel=\"alternate\" href=\"https://example.com/atom-1\"/></entry>";
        check("atom_link_href_extracted", atomLink(entry), "https://example.com/atom-1");
    }

    // --- CDATA-wrapped title ---
    {
        const std::string block = "<item><title><![CDATA[Title & More]]></title></item>";
        check("cdata_title_unwrapped_and_unescaped", tag(block, "title"), "Title & More");
    }

    // --- non-HTTP(S) scheme must be rejected by the caller (mirrors RssCollector's own check) ---
    {
        const auto canonical = canonicalize("javascript:alert(1)");
        checkTrue("non_http_scheme_detectable",
                  canonical.rfind("http://", 0) != 0 && canonical.rfind("https://", 0) != 0,
                  "canonicalize() does not itself reject non-http(s) schemes; caller must check the prefix");
    }

    std::cout << "\n" << failures << " failing check(s) out of the documented edge cases.\n";
    return 0;  // Diagnostic tool: report findings, never fail the build.
}
