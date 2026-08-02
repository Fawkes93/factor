from factor.crypto.tokens import decrypt_token, encrypt_token, generate_fernet_key


def test_encrypt_decrypt_roundtrip_fernet_key():
    key = generate_fernet_key()
    ct = encrypt_token("secret-access-token", key)
    assert ct != "secret-access-token"
    assert decrypt_token(ct, key) == "secret-access-token"


def test_encrypt_decrypt_passphrase():
    key = "my-dev-passphrase-not-for-prod"
    ct = encrypt_token("refresh-xyz", key)
    assert decrypt_token(ct, key) == "refresh-xyz"


def test_wrong_key_fails():
    key1 = generate_fernet_key()
    key2 = generate_fernet_key()
    ct = encrypt_token("tok", key1)
    try:
        decrypt_token(ct, key2)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "decrypt" in str(e).lower() or "key" in str(e).lower()
