"""Read ARGUS's existing Telegram response with MarketPulse on the hosted runner."""
import json
import sys
from bs4 import BeautifulSoup


def read_page(html, root):
    sys.path.insert(0, str(root))
    from platforms.telegram_scraper import TelegramScraper
    # The upstream constructor opens an HTTP client. The DOM parser is stateless.
    parser = TelegramScraper.__new__(TelegramScraper)
    soup = BeautifulSoup(html, "html.parser")
    return [parser._extract_post(node, i) for i, node in enumerate(soup.select(".tgme_widget_message[data-post]"), 1)]


if __name__ == "__main__":
    print(json.dumps(read_page(sys.stdin.buffer.read().decode("utf-8"), sys.argv[1])))
