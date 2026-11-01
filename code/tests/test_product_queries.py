"""Combination filtering, visibility, exact prices, and stable pagination checks."""

from datetime import datetime

import pytest

from app.database import get_session
from app.models import Product, ProductCategory, ProductStatus
from tests.helpers import login_as


@pytest.fixture(name="catalogue")
def catalogue_fixture(app, accounts):
    """Prepare public and private records with known prices and tied publication timestamps."""
    with app.app_context():
        database = get_session()
        products = [
            Product(
                seller_id=accounts["member" if index % 2 == 0 else "other"],
                name=f"公开商品{index:02d}", category=ProductCategory.DIGITAL,
                price_cents=1000 + index, description="公开描述",
                status=ProductStatus.PUBLISHED, published_at=datetime(2026, 10, 30),
            ) for index in range(14)
        ]
        products.extend([
            Product(seller_id=accounts["member"], name="本人秘密草稿", status=ProductStatus.DRAFT),
            Product(seller_id=accounts["other"], name="他人秘密草稿", status=ProductStatus.DRAFT),
            Product(
                seller_id=accounts["member"], name="待审记录", category=ProductCategory.BOOKS,
                price_cents=500, description="待审核",
                status=ProductStatus.PENDING_REVIEW,
            ),
            Product(
                seller_id=accounts["member"], name="100%_键盘", category=ProductCategory.BOOKS,
                price_cents=29, description="按分保存价格",
                status=ProductStatus.PUBLISHED, published_at=datetime(2026, 10, 29),
            ),
        ])
        database.add_all(products)
        database.commit()
    return accounts


def test_public_filters_prices_and_stable_pagination(app, catalogue):
    """Combined conditions and tied-time sorting are evaluated before server pagination."""
    assert catalogue
    client = app.test_client()
    first = client.get(
        "/products?keyword=公开商品&category=DIGITAL&per_page=9999",
    ).get_data(as_text=True)
    assert "共 14 件商品" in first
    assert "公开商品13" in first and "公开商品02" in first
    assert "公开商品01" not in first and "公开商品00" not in first
    assert first.index("公开商品13") < first.index("公开商品12")
    second = client.get("/products?keyword=公开商品&category=DIGITAL&page=2").get_data(as_text=True)
    assert "公开商品01" in second and "公开商品00" in second
    assert "公开商品02" not in second
    filtered = client.get(
        "/products?keyword=公开商品&category=DIGITAL&min_price=10.05&max_price=10.07",
    ).get_data(as_text=True)
    assert "共 3 件商品" in filtered
    assert all(f"公开商品{index:02d}" in filtered for index in (5, 6, 7))
    assert "公开商品04" not in filtered and "公开商品08" not in filtered


def test_wildcards_are_literal_and_queries_do_not_leak_drafts(app, catalogue):
    """LIKE wildcards and forged scope parameters cannot broaden the visible data."""
    assert catalogue
    client = app.test_client()
    response = client.get("/products", query_string={"keyword": "%_"})
    html = response.get_data(as_text=True)
    assert "100%_键盘" in html and "共 1 件商品" in html
    assert "公开商品" not in html
    hidden = client.get("/products?status=DRAFT&scope=admin&seller_id=1").get_data(as_text=True)
    assert "秘密草稿" not in hidden
    assert "共 0 件商品" in hidden
    injection = client.get("/products", query_string={"keyword": "%' OR 1=1 --"})
    assert injection.status_code == 200
    assert "共 0 件商品" in injection.get_data(as_text=True)
    exact = client.get("/products?min_price=0.29&max_price=0.29").get_data(as_text=True)
    assert "共 1 件商品" in exact and "¥0.29" in exact


@pytest.mark.parametrize("query", [
    "min_price=30&max_price=10", "min_price=NaN", "max_price=-1",
    "category=EVIL", "status=UNKNOWN", "page=0", "page=abc",
])
def test_invalid_query_is_explained_without_clearing_input(app, query):
    """Malformed ranges, enum values, and pages are errors rather than unfiltered results."""
    response = app.test_client().get("/products?keyword=保留关键词&" + query)
    assert response.status_code == 400
    assert "保留关键词" in response.get_data(as_text=True)
    assert 'role="alert"' in response.get_data(as_text=True)


def test_personal_and_admin_queries_keep_their_scopes(app, catalogue):
    """Ownership and ADMIN access apply independently of supplied filter fields."""
    assert catalogue
    member = app.test_client()
    assert login_as(member, "member").status_code == 303
    html = member.get("/my/products?status=DRAFT&seller_id=3").get_data(as_text=True)
    assert "本人秘密草稿" in html and "他人秘密草稿" not in html
    assert member.get("/admin/products").status_code == 403
    assert app.test_client().get("/my/products").status_code == 303
    administrator = app.test_client()
    assert login_as(administrator, "reviewer").status_code == 303
    pending = administrator.get("/admin/products").get_data(as_text=True)
    assert "待审记录" in pending and "秘密草稿" not in pending
    drafts = administrator.get("/admin/products?status=DRAFT").get_data(as_text=True)
    assert "本人秘密草稿" in drafts and "他人秘密草稿" in drafts


def test_product_text_is_escaped_and_empty_results_are_clear(app, catalogue):
    """Seller text is rendered as text rather than executable HTML."""
    with app.app_context():
        database = get_session()
        database.add(Product(
            seller_id=catalogue["member"], name="<script>alert(1)</script>",
            category=ProductCategory.OTHER, price_cents=100, description="<img onerror=alert(1)>",
            status=ProductStatus.PUBLISHED, published_at=datetime(2026, 10, 31),
        ))
        database.commit()
    client = app.test_client()
    html = client.get("/products").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "暂无符合条件的商品" in client.get(
        "/products?keyword=不存在的匹配条件",
    ).get_data(as_text=True)
