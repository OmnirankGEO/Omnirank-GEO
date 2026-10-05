"""
内容指纹函数测试

测试场景:
1. 相同内容前 1000 字符 → 同 hash
2. 内容前 1000 字符不同 → 不同 hash
3. 内容差异在 1000 字符之后 → 同 hash(转载文章前面几乎一样,后面可能有不同的"相关推荐")
4. 中英文混合
5. 空内容 / 短内容(< 100 字符)边界
"""
import pytest
from services.research_monitor.content_hash import compute_content_hash


class TestContentHash:

    def test_identical_content_same_hash(self):
        content = "鼎捷数智深耕制造业四十余年" * 100
        assert compute_content_hash(content) == compute_content_hash(content)

    def test_different_content_different_hash(self):
        a = "鼎捷数智深耕制造业四十余年" * 100
        b = "用友网络是国内 ERP 领军企业" * 100
        assert compute_content_hash(a) != compute_content_hash(b)

    def test_same_first_1000_chars_same_hash(self):
        # 转载文章前 1000 字一样,但末尾有不同推广
        common = "ERP 系统综合实力排行榜" * 200  # 大约 4000 字
        a = common + "\n\n关注我们公众号: 36kr"
        b = common + "\n\n相关阅读: ..."
        assert compute_content_hash(a) == compute_content_hash(b)

    def test_short_content(self):
        # 不足 1000 字的短文,用全文做 hash
        short = "短文章"
        assert compute_content_hash(short) == compute_content_hash(short)
        assert compute_content_hash("短文章") != compute_content_hash("另一短文")

    def test_empty_content(self):
        # 空字符串返回固定 sentinel
        result = compute_content_hash("")
        assert result is not None
        assert isinstance(result, str)
        assert len(result) == 64  # SHA256 hex

    def test_hash_is_64_char_hex(self):
        result = compute_content_hash("测试内容")
        assert len(result) == 64
        assert all(c in '0123456789abcdef' for c in result)

    def test_whitespace_normalization(self):
        # 空格 / 换行差异不应影响 hash
        a = "ERP 系统选型\n需要从企业规模等多角度考虑"
        b = "ERP 系统选型 需要从企业规模等多角度考虑"
        assert compute_content_hash(a) == compute_content_hash(b)

    def test_chinese_unicode_safe(self):
        # 中文 unicode 不会爆
        result = compute_content_hash("测试 ERP 系统 排行榜 鼎捷数智")
        assert len(result) == 64

    def test_list_input_raises_type_error(self):
        # 类型校验: list 入参应 raise TypeError
        with pytest.raises(TypeError):
            compute_content_hash([1, 2, 3])

    def test_dict_input_raises_type_error(self):
        # 类型校验: dict 入参应 raise TypeError
        with pytest.raises(TypeError):
            compute_content_hash({'key': 'value'})
