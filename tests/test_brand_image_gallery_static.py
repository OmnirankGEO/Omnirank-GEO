from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GALLERY = ROOT / "frontend" / "src" / "components" / "brand" / "BrandImageGallery.tsx"
WRITING_HALL = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"
BRAND_DETAIL = ROOT / "frontend" / "src" / "pages" / "Brand" / "BrandDetailPage.tsx"


def _gallery_source() -> str:
    return GALLERY.read_text(encoding="utf-8")


def test_brand_image_gallery_supports_multi_select_delete():
    src = _gallery_source()

    assert "onAssetsChange" in src
    assert "selectedAssetIds" in src
    assert "toggleAssetSelection" in src
    assert "handleBatchDelete" in src
    assert "删除选中" in src
    assert "全选" in src
    assert "Promise.all" in src
    assert "/api/brand-images/asset/${assetId}" in src


def test_writing_hall_and_brand_detail_share_brand_image_gallery():
    writing_src = WRITING_HALL.read_text(encoding="utf-8")
    detail_src = BRAND_DETAIL.read_text(encoding="utf-8")

    assert "onAssetsChange={setBrandImageAssets}" in writing_src
    assert "<BrandImageGallery brandId={brandId} embedded />" in detail_src
    assert "<BrandImageGallery brandId={brandId} />" in detail_src
