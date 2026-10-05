"""品牌识别工具模块"""

from .brand_account_identifier import (
    BrandAccountIdentifier,
    identify_brand_accounts,
    get_brand_content_for_scoring
)

__all__ = [
    'BrandAccountIdentifier',
    'identify_brand_accounts',
    'get_brand_content_for_scoring'
]
