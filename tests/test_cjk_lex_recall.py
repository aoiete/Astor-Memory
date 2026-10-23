"""中文词法召回的 tokenizer 行为测试。

v1.16.88 (2026-10-22): 中文 unigram + **bigram**。

背景: 实测发现旧实现只发单字, 中文几乎没有区分度 ("闸" "门" "防"
这类单字满语料都是), 于是加了零依赖的 bigram 相邻二字组
(不引 jieba, 见 lex_index._tokenize docstring 里的理由)。

注意: 之前有一版断言写的是"只有单字", bigram 上线后就过时了 ——
是测试失败暴露的, 已按实测行为重写。锁住新行为, 防止有人误改回去。
"""
import pytest


def _tok(s):
    from astor_memory.nest.lex_index import _tokenize
    return _tokenize(s)


def test_english_unchanged():
    """英文路径完全不受 bigram 影响。"""
    assert _tok("OpenD RemoteClose hello world") == [
        "opend", "remoteclose", "hello", "world"]


def test_english_stopwords_dropped():
    toks = _tok("the user wants this and that")
    assert "the" not in toks and "and" not in toks
    assert "user" in toks


def test_cjk_unigrams_present():
    """中文单字仍在 (短 query 仍有召回面)。"""
    toks = _tok("配额打满")
    for ch in "配额打满":
        assert ch in toks


def test_cjk_bigrams_present():
    """中文 bigram 是新增的 —— 单字没区分度, 二字组才有。"""
    toks = _tok("配额打满")
    assert "配额" in toks and "打满" in toks


def test_cjk_bigram_does_not_cross_word_boundary():
    """bigram 不能跨英文/空格拼接。"""
    toks = _tok("会话 expired 过期")
    assert "话e" not in toks
    assert "话expired" not in toks
    assert "会话" in toks and "过期" in toks


def test_bigram_improves_discrimination():
    """核心动机: '闸门' 这种组合有区分度, 单个 '闸' 没有。"""
    toks = _tok("相关性闸门")
    assert "闸" in toks
    assert "闸门" in toks


def test_mixed_cjk_and_english():
    toks = _tok("OpenD 会话过期导致 RemoteClose")
    assert "opend" in toks and "remoteclose" in toks
    assert "会话" in toks and "过期" in toks


def test_single_cjk_char_no_bigram():
    """单个中文字没有前一个字, 不该凭空造 bigram。"""
    assert _tok("配") == ["配"]


def test_empty_and_punctuation():
    assert _tok("") == []
    assert _tok("!!! ???") == []


@pytest.mark.parametrize("q", ["会话过期", "看门狗误杀", "配额打满", "相关性闸门"])
def test_cjk_token_count_is_unigrams_plus_bigrams(q):
    """n 字中文 -> n 个单字 + (n-1) 个 bigram = 2n-1。"""
    assert len(_tok(q)) == 2 * len(q) - 1
