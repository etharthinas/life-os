from app import contacts

CARD = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:박;인하;;;\r\nFN:박인하\r\n"
    "item1.TEL;type=CELL:+82 10-1234-5678\r\nEMAIL;type=INTERNET:inha@example.com\r\n"
    "BDAY;X-APPLE-OMIT-YEAR=1604:1604-05-29\r\nORG:IVF;\r\nNOTE:line one\\nline two\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:2.1\r\nN:Liu;Aaron\r\nTEL;CELL:010 9999 0000\r\n"
    "NOTE;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:=EC=95=88=EB=85=95\r\nEND:VCARD\r\n"
)


def test_parse_vcards():
    a, b = contacts.parse(CARD)
    assert a["name"] == "박인하" and a["phones"] == ["+82 10-1234-5678"] and a["emails"] == ["inha@example.com"]
    assert a["birthday"] == (None, 5, 29) and a["org"] == "IVF" and a["note"] == "line one\nline two"
    assert b["name"] == "Aaron Liu" and b["note"] == "안녕"


def test_phone_key_ignores_format_and_country_code():
    assert contacts.phone_key("+82 10-1234-5678") == contacts.phone_key("010.1234.5678") == "01012345678"


def test_vcard_round_trip():
    p = {"name": "박인하", "contact": {"phones": ["010-1234-5678"], "emails": ["a@b.c"]}, "fields": {},
         "birth_year": None, "birth_month": 5, "birth_day": 29, "about": "대표님"}
    (c,) = contacts.parse(contacts.vcard(p, ["IVF"]))
    assert c["name"] == "박인하" and c["phones"] == ["010-1234-5678"] and c["birthday"] == (None, 5, 29)
    assert c["org"] == "IVF" and c["note"] == "대표님"


def test_note_phone_formats():
    p = {"contact": {"phones": ["010-1234-5678"]}, "fields": {"Phone": ["tel:01012345678", "[010 9999 0000](tel:01099990000)"]}}
    assert contacts.phones_of(p) == ["010-1234-5678", "010 9999 0000"]
