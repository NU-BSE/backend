from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

output = Path("secrets/jit-ed25519-private.pem")
output.parent.mkdir(parents=True, exist_ok=True)
if output.exists():
    raise SystemExit(f"Refusing to overwrite {output}")
key = Ed25519PrivateKey.generate()
output.write_bytes(
    key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
)
print(f"Created {output}")
