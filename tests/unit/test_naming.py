import pytest

from reels.domain.naming import SLUG_MAX_LEN, slugify


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Demo", "demo"),
        ("  Müller & Söhne GmbH ", "mueller-soehne-gmbh"),
        ("Straße 42", "strasse-42"),
        ("Café Crème", "cafe-creme"),
        ("ÄRZTE--Praxis!!", "aerzte-praxis"),
        ("already-a-slug", "already-a-slug"),
    ],
)
def test_slugify(name: str, slug: str) -> None:
    assert slugify(name) == slug


def test_slugify_truncates_without_trailing_dash() -> None:
    slug = slugify("a" * (SLUG_MAX_LEN - 1) + " b")
    assert len(slug) <= SLUG_MAX_LEN
    assert not slug.endswith("-")


@pytest.mark.parametrize("name", ["", "   ", "!!!", "😀"])
def test_slugify_rejects_unusable_names(name: str) -> None:
    with pytest.raises(ValueError):
        slugify(name)
