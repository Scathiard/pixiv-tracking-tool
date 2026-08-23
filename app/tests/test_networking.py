from src.networking import access_urls


def test_access_urls_show_ipv4_and_routable_ipv6_without_duplicates():
    assert access_urls(1919, [
        "127.0.0.1",
        "192.168.1.20",
        "192.168.1.20",
        "fe80::1%12",
        "fd12:3456::20",
        "2001:db8::20",
    ]) == [
        ("局域网 IPv4", "http://192.168.1.20:1919"),
        ("IPv6", "http://[fd12:3456::20]:1919"),
        ("IPv6", "http://[2001:db8::20]:1919"),
    ]
