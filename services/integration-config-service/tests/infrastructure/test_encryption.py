from app.infrastructure.config.encryption import decrypt, encrypt


class TestEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        plain = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        token = encrypt(plain)
        assert token != plain
        assert decrypt(token) == plain

    def test_different_plaintexts_produce_different_tokens(self):
        assert encrypt("secret-a") != encrypt("secret-b")
