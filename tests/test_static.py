import ast
from pathlib import Path
root = Path(__file__).resolve().parents[1]
booking = (root / "web" / "booking.py").read_text(encoding="utf-8")
relay = (root / "relay" / "main.py").read_text(encoding="utf-8")
for name, source in (("booking", booking), ("relay", relay)):
    ast.parse(source)
    print(name, "syntax OK")
assert "BOOKING_TEST_CALLER_PHONE" not in booking
assert "caller!=allowed" not in booking
assert "'/internal/book-test'" in relay
assert "def reservation_save" not in relay
assert "preemptible': False" in relay
print("Static checks OK")
