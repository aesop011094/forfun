"""Regression: the actual UI helper must display 1X2 totals of exactly 100.0%."""
import json
from pathlib import Path
import random
import shutil
import subprocess

import pytest


def test_actual_typescript_probability_formatter_distributes_all_remaining_units():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the frontend probability formatter')
    module = (Path(__file__).resolve().parents[1] / 'web/src/types.ts').as_uri()
    generator = random.Random(42)
    cases = [[.3333, .3333, .3334], [.4518, .3188, .2294], [.99999, .000005, .000005], [1, 1, 1]]
    cases += [[generator.random() for _ in range(3)] for _ in range(100)]
    program = """import { roundedProbabilities } from MODULE;
const inputs = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(inputs.map(values => roundedProbabilities(values))));
""".replace('MODULE', json.dumps(module))
    result = subprocess.run([node, '--experimental-strip-types', '--input-type=module', '-e', program, json.dumps(cases)],
                            capture_output=True, text=True, check=True)
    displayed = json.loads(result.stdout)
    for original, values in zip(cases, displayed):
        percentages = [float(value.removesuffix('%')) for value in values]
        assert sum(percentages) == pytest.approx(100, abs=1e-8), (original, values)
        assert all(0 <= value <= 100 for value in percentages)
        normalized = [value / sum(original) * 100 for value in original]
        assert all(abs(actual - target) <= .1 + 1e-8 for actual, target in zip(percentages, normalized))
