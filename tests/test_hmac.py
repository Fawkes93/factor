from factor.inbound.auth import sign_body, verify_signature


def test_sign_and_verify():
    secret = "whsec_test"
    body = b'{"hello":"world"}'
    sig = sign_body(secret, body)
    assert sig.startswith("sha256=")
    assert verify_signature(secret, body, sig)
    assert not verify_signature(secret, body, "sha256=deadbeef")
    assert not verify_signature(secret, b"other", sig)
    assert not verify_signature("", body, sig)
