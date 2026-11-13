"""Fail the private image build if a browser parser's runtime asset is absent."""
from pathlib import Path
import re
import subprocess
import sys

ADAPTERS = ('Instagram', 'TikTok', 'Vk', 'Ok', 'Threads', 'X')


def check(root):
    for name in ADAPTERS:
        path = root / 'backend/src/platforms' / (name + 'Adapter.cpp')
        text = path.read_text(encoding='utf-8')
        if not re.search(r'extractionExpression\s*=\s*R"JS\((.*?)\)JS"', text, re.S):
            raise ValueError('browser_extraction_asset_missing: ' + name)
    # --help exits before collection, but argparse validates preceding options.
    # Exercise the actual CLI boundary used by the supervisor at image build.
    subprocess.run([sys.executable,str(root/'ops/collect/run.py'),'--sources','unused',
                    '--concurrency','6','--stream-results','--batch-seconds','105',
                    '--source-timeout','65','--help'],check=True,capture_output=True,timeout=30)


if __name__ == '__main__':
    check(Path(__file__).resolve().parents[2])
    print('Browser extraction assets verified')
