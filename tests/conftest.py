import os
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture
def cfg():
    with open(os.path.join(ROOT, "config.yaml")) as f:
        c = yaml.safe_load(f)
    # Tests check the pricing/tier logic against fixed lines, so they don't
    # break when the thresholds in config.yaml are tuned.
    cats = c["categories"]
    cats["9mm"].update(good=0.25, great=0.23, exceptional=0.21)
    cats["12ga_a"].update(good=0.58, great=0.50, exceptional=0.45, min_velocity=1290)
    cats["12ga_b"].update(good=0.42, great=0.36)
    cats["762x54r"].update(good=0.55, great=0.45, exceptional=0.40, can_440_good_total=240, can_440_great_total=200)
    return c


def fixture(name):
    with open(os.path.join(ROOT, "tests", "fixtures", name)) as f:
        return f.read()
