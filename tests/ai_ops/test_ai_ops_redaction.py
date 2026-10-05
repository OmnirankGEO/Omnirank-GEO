"""services/ai_ops/redaction.py 测试(纯函数,无 DB)。"""
from services.ai_ops import redaction

_REDACTED = "«REDACTED»"


def test_redacts_database_url():
    out = redaction.redact("DATABASE_URL=postgresql://geo_admin:s3cr3tpw@db:5432/geo")
    assert "s3cr3tpw" not in out
    assert _REDACTED in out


def test_redacts_connection_string_password_inline():
    out = redaction.redact("连接串是 postgresql://user:supersecret@host:5432/db 记得别泄露")
    assert "supersecret" not in out


def test_redacts_wechat_pay_and_generic_keys():
    text = "WX_APIV3_KEY=abcd1234efgh5678\nDASHSCOPE_API_KEY: sk-abc\nSOME_SECRET=xyz"
    out = redaction.redact(text)
    assert "abcd1234efgh5678" not in out
    assert "xyz" not in out


def test_redacts_pem_block():
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADAN...\n-----END PRIVATE KEY-----"
    out = redaction.redact(f"private key follows:\n{pem}\nend")
    assert "MIIEvQIBADAN" not in out
    assert _REDACTED in out


def test_redacts_jwt_and_sk_token():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123DEF"
    out = redaction.redact(f"token={jwt} sk-ABCDEFGHIJKLMNOP1234")
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "sk-ABCDEFGHIJKLMNOP1234" not in out


def test_redacts_phone_and_bankcard():
    out = redaction.redact("联系 13812345678 卡号 6222021234567890123")
    assert "13812345678" not in out
    assert "6222021234567890123" not in out


def test_keeps_normal_text():
    out = redaction.redact("报价页确认按钮点不了,报 500")
    assert "报价页确认按钮点不了" in out


def test_redacts_json_form_secret():
    # P2-1:JSON 形态密钥也要脱敏
    out = redaction.redact('{"WX_APIV3_KEY":"abc123-not-redacted"}')
    assert "abc123-not-redacted" not in out
    out2 = redaction.redact('{"api_token": "eyzzz", "DOUBAO_ENDPOINT_ID": "ep-123"}')
    assert "eyzzz" not in out2
    assert "ep-123" not in out2


def test_redact_dict_by_key():
    # P2-1:按 key 脱敏(值即使不含明显密钥特征也删)
    out = redaction.redact_dict({
        "WX_APIV3_KEY": "abc123",
        "note": "报价页点不动",
        "nested": {"api_token": "xyz"},
    })
    assert out["WX_APIV3_KEY"] == "«REDACTED»"
    assert out["note"] == "报价页点不动"
    assert out["nested"]["api_token"] == "«REDACTED»"


def test_redact_dict_recurses():
    out = redaction.redact_dict({
        "msg": "DATABASE_URL=postgresql://u:pw123@h/db",
        "nested": {"token": "eyJx.eyJy.zzz"},
        "list": ["sk-ABCDEFGHIJKLMNOP1234", "ok"],
        "num": 42,
    })
    assert "pw123" not in out["msg"]
    assert "eyJx.eyJy.zzz" not in out["nested"]["token"]
    assert "sk-ABCDEFGHIJKLMNOP1234" not in out["list"][0]
    assert out["num"] == 42
