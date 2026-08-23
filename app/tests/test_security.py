from pathlib import Path

import pytest

from src.security import (
    NetworkPolicy,
    is_same_origin,
    resolve_image_path,
    sanitize_caption,
)


def test_resolve_image_path_accepts_only_files_below_root(tmp_path: Path):
    images = tmp_path / "images"
    images.mkdir()
    artwork = images / "Artist 1" / "100_p0.jpg"
    artwork.parent.mkdir()
    artwork.write_bytes(b"image")
    secret = tmp_path / "data" / "pixiv_token.json"
    secret.parent.mkdir()
    secret.write_text("secret", encoding="utf-8")

    assert resolve_image_path(images, "Artist 1/100_p0.jpg") == artwork.resolve()
    for attack in (
        "../data/pixiv_token.json",
        "..\\data\\pixiv_token.json",
        "Artist 1/../../data/pixiv_token.json",
        "%2e%2e%2fdata%2fpixiv_token.json",
        "%252e%252e%252fdata%252fpixiv_token.json",
    ):
        assert resolve_image_path(images, attack) is None


def test_resolve_image_path_rejects_missing_directories_and_directories(tmp_path: Path):
    images = tmp_path / "images"
    images.mkdir()
    (images / "folder").mkdir()

    assert resolve_image_path(images, "missing.jpg") is None
    assert resolve_image_path(images, "folder") is None


def test_caption_sanitizer_keeps_formatting_but_removes_executable_content():
    raw = """
    <p onclick="steal()">正文 <strong>加粗</strong></p>
    <script>alert(1)</script>
    <a href="javascript:alert(2)" target="_blank">坏链接</a>
    <a href="https://www.pixiv.net/artworks/1" target="_blank">安全链接</a>
    <img src=x onerror=alert(3)>
    """

    clean = sanitize_caption(raw)

    assert "正文" in clean
    assert "<strong>加粗</strong>" in clean
    assert "script" not in clean.lower()
    assert "onclick" not in clean.lower()
    assert "javascript:" not in clean.lower()
    assert "<img" not in clean.lower()
    assert 'href="https://www.pixiv.net/artworks/1"' in clean
    assert 'rel="noopener noreferrer"' in clean


def test_network_policy_allows_same_lan_ipv4_and_ipv6_but_not_external():
    policy = NetworkPolicy.from_interfaces(
        [
            ("192.168.50.10", "255.255.255.0"),
            ("2001:db8:1234:5678::10", "ffff:ffff:ffff:ffff::"),
        ]
    )

    assert policy.allows("127.0.0.1")
    assert policy.allows("192.168.50.77")
    assert not policy.allows("192.168.51.77")
    assert policy.allows("2001:db8:1234:5678::99")
    assert not policy.allows("2001:db8:9999::1")
    assert policy.allows("::ffff:192.168.50.77")


def test_network_policy_uses_ipv6_lan_prefix_when_windows_omits_netmask():
    policy = NetworkPolicy.from_interfaces(
        [("2408:820c:c31b:fe80:437c:c16f:fae9:496b", None)]
    )

    assert policy.allows("2408:820c:c31b:fe80::1919")
    assert not policy.allows("2408:820c:c31b:fe81::1919")


def test_network_policy_does_not_expand_ipv6_loopback_to_a_subnet():
    policy = NetworkPolicy.from_interfaces([("::1", None)])

    assert policy.allows("::1")
    assert not policy.allows("::2")


@pytest.mark.parametrize(
    ("origin", "host", "expected"),
    [
        ("http://192.168.1.4:1919", "192.168.1.4:1919", True),
        ("http://[fd00::4]:1919", "[fd00::4]:1919", True),
        ("https://evil.example", "192.168.1.4:1919", False),
        ("null", "192.168.1.4:1919", False),
    ],
)
def test_same_origin(origin: str, host: str, expected: bool):
    assert is_same_origin(origin, host) is expected
