"""OSS helper 测试 (mock oss2, 不真连阿里云)"""
import pytest
from unittest.mock import patch, MagicMock
from services.research_monitor.oss_helper import (
    upload_markdown,
    download_markdown,
    generate_oss_key_for_article,
    OssHelper,
)


@pytest.fixture(autouse=True)
def _set_oss_env(monkeypatch):
    monkeypatch.setenv('OSS_ACCESS_KEY_ID', 'test-id')
    monkeypatch.setenv('OSS_ACCESS_KEY_SECRET', 'test-secret')
    monkeypatch.setenv('OSS_RESEARCH_ARTICLES_BUCKET', 'omnirank-research-articles')
    monkeypatch.setenv('OSS_RESEARCH_ARTICLES_ENDPOINT', 'oss-cn-shenzhen-internal.aliyuncs.com')
    # 每个测试前重置单例,避免 env 互相污染
    OssHelper._instance = None
    yield
    OssHelper._instance = None


class TestGenerateOssKey:

    def test_basic_path(self):
        """基础路径生成: prefix/yyyy-mm/domain/url_hash.md"""
        key = generate_oss_key_for_article(
            prefix='raw',
            year_month='2026-05',
            domain='digiwin.com',
            url_hash='a' * 40,
        )
        assert key == 'raw/2026-05/digiwin.com/' + 'a' * 40 + '.md'

    def test_cleaned_prefix(self):
        key = generate_oss_key_for_article(
            prefix='cleaned',
            year_month='2026-05',
            domain='36kr.com',
            url_hash='b' * 40,
        )
        assert key.startswith('cleaned/')
        assert '36kr.com' in key

    def test_special_domain_chars(self):
        """域名含特殊字符不影响"""
        key = generate_oss_key_for_article(
            prefix='raw',
            year_month='2026-05',
            domain='sub.example-domain.co.uk',
            url_hash='c' * 40,
        )
        assert 'sub.example-domain.co.uk' in key


class TestUploadDownload:

    def test_upload_markdown_success(self):
        with patch('services.research_monitor.oss_helper.oss2') as mock_oss2:
            mock_bucket = MagicMock()
            mock_bucket.put_object.return_value = MagicMock(status=200)
            mock_oss2.Bucket.return_value = mock_bucket
            mock_oss2.Auth.return_value = MagicMock()

            result = upload_markdown('raw/2026-05/test/abc.md', '# 测试文章')
            # I1: upload_markdown 返 dict 不再返 bool
            assert result['ok'] is True
            assert result['oss_key'] == 'raw/2026-05/test/abc.md'
            assert result['error'] is None
            mock_bucket.put_object.assert_called_once()

    def test_upload_markdown_failure(self):
        with patch('services.research_monitor.oss_helper.oss2') as mock_oss2:
            mock_bucket = MagicMock()
            mock_bucket.put_object.side_effect = Exception("OSS error")
            mock_oss2.Bucket.return_value = mock_bucket
            mock_oss2.Auth.return_value = MagicMock()

            result = upload_markdown('raw/2026-05/test/abc.md', '# 测试文章')
            assert result['ok'] is False
            assert result['oss_key'] == 'raw/2026-05/test/abc.md'
            assert 'OSS error' in result['error']

    def test_download_markdown_success(self):
        with patch('services.research_monitor.oss_helper.oss2') as mock_oss2:
            mock_bucket = MagicMock()
            mock_object = MagicMock()
            mock_object.read.return_value = b'# \xe6\xb5\x8b\xe8\xaf\x95\xe6\x96\x87\xe7\xab\xa0'  # UTF-8 '# 测试文章'
            mock_bucket.get_object.return_value = mock_object
            mock_oss2.Bucket.return_value = mock_bucket
            mock_oss2.Auth.return_value = MagicMock()

            content = download_markdown('raw/2026-05/test/abc.md')
            assert content == '# 测试文章'

    def test_download_markdown_not_found(self):
        with patch('services.research_monitor.oss_helper.oss2') as mock_oss2:
            mock_bucket = MagicMock()
            mock_bucket.get_object.side_effect = Exception("NoSuchKey")
            mock_oss2.Bucket.return_value = mock_bucket
            mock_oss2.Auth.return_value = MagicMock()

            content = download_markdown('raw/non-existent.md')
            assert content is None

    def test_oss_helper_singleton(self):
        """OssHelper 模块级单例,多次调用不重建 Bucket"""
        with patch('services.research_monitor.oss_helper.oss2') as mock_oss2:
            mock_oss2.Auth.return_value = MagicMock()
            mock_oss2.Bucket.return_value = MagicMock()

            helper1 = OssHelper.get_instance()
            helper2 = OssHelper.get_instance()
            assert helper1 is helper2

    def test_missing_oss_credentials(self, monkeypatch):
        """缺 OSS 凭据 → upload 返 dict ok=False"""
        monkeypatch.delenv('OSS_ACCESS_KEY_ID', raising=False)
        # OssHelper 单例可能已经被前面测试初始化, 强制重置
        OssHelper._instance = None
        result = upload_markdown('raw/x.md', '# x')
        assert result['ok'] is False
        assert 'OSS_ACCESS_KEY_ID' in result['error'] or '未设置' in result['error']
