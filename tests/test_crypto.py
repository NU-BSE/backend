from app.schemas.attestation import TransferAction
from app.services.crypto import canonicalize_model, compute_attestation_request_hash, compute_payload_hash


def test_canonical_action_and_hash_are_stable() -> None:
    action = TransferAction(
        type="transfer",
        amount=1250,
        currency="KZT",
        toAccountId="acc-7",
        memo="test",
    )
    canonical = canonicalize_model(action)
    assert canonical == '{"amount":1250,"currency":"KZT","memo":"test","toAccountId":"acc-7","type":"transfer"}'
    assert len(compute_payload_hash(canonical)) == 64
    assert "=" not in compute_attestation_request_hash("nonce", compute_payload_hash(canonical))
