"""Benchmark-only wrapper: real Web app plus one route that runs a real insurance turn."""
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'web'))
import main  # noqa: E402
from flask import jsonify  # noqa: E402

app = main.app
_n = itertools.count()


@app.get('/_bench/turn')
def bench_turn():
    reply = main.converse({'business_id': 'INS-BIZ-001', 'sector': 'insurance'}, 'Voice', '+34600111222',
                             '¿Qué cubre mi póliza?', f'CA-bench:{next(_n)}', sector='insurance')
    return jsonify(reply=reply)
