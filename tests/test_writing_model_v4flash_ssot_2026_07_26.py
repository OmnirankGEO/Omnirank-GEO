"""
写作模型 SSOT 判别测试(2026-07-26)

背景:settings.json 是 gitignored 的运行时配置。迁到"release 目录 + 打标镜像"部署模型后,
没有任何挂载把它送进容器,于是 writing/llm_utils.get_llm_config() 的 settings_file.exists()
恒 False,写作链**静默**落回 dashscope/qwen3.6-plus —— 而既定写作模型是官方线 Flash 档。
无告警、无日志,漂移隐身数月,代价是品牌名命中率 6.8 → 19.2 全没拿到。

因此本文件同时守两层,缺一不可:
  1. 代码层:配置缺失时,写作模块的默认值必须**就是**既定模型(而不是"随便一个能跑的")。
  2. 部署层:compose 必须真的把 settings.json 挂进容器,否则配置文件永远是死的。

这两条任意一条被人改回去,下面的用例就红。
"""

import io
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# 🔴 [WO_206 c1b 翻面] 原来这里写死 "deepseek-v4-flash"。官方 2026-09-13 把 Flash 档
#    改名 deepseek-flash,于是这条判据从「防漂移」变成了「钉住一个过期名」——
#    它会和正确的修法**互斥**。所以改成取常量:
#      · 本判据守的那件事一个字没变 —— 配置缺失时不许静默落回 dashscope/qwen;
#      · 但「既定模型叫什么」只剩一个出处(config/deepseek_models),
#        再改名时这里跟着走,不会再有"判据钉着旧名"这种自相矛盾。
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

EXPECTED_PROVIDER = "deepseek"
EXPECTED_MODEL = DEEPSEEK_OFFICIAL_FLASH


def _get_config(tmp_path, monkeypatch, task_type, module):
    """把 settings.json 的查找路径指向一个空目录,复现"生产读不到配置"的真实处境。"""
    import writing.llm_utils as llm_utils

    fake_pkg_dir = tmp_path / "writing"
    fake_pkg_dir.mkdir()
    monkeypatch.setattr(llm_utils, "__file__", str(fake_pkg_dir / "llm_utils.py"))
    assert not (tmp_path / "settings.json").exists(), "前提:配置文件确实不存在"
    return llm_utils.get_llm_config(task_type, module=module)


@pytest.mark.parametrize("task_type", ["geo_article", "title_generation", "某个还没配的新任务"])
def test_writing_defaults_to_the_official_flash_constant_when_settings_missing(
        tmp_path, monkeypatch, task_type):
    """配置缺失时写作链必须仍是**官方线 Flash 档常量** —— 这是本次漂移的直接防线。

    🔴 判据名跟着断言一起改:原名写着 v4_flash,而断言已经不查那个名字了 ——
       名字说一件事、断言查另一件事,是本仓踩过的坑。

    未列出的任务名也一并覆盖:漂移不只发生在已配任务上,新增任务同样会吃默认值。
    """
    _api_url, _api_key, model, provider = _get_config(tmp_path, monkeypatch, task_type, "writing")
    assert (provider, model) == (EXPECTED_PROVIDER, EXPECTED_MODEL), (
        f"写作任务 {task_type} 在配置缺失时落到 {provider}/{model},"
        f"应为 {EXPECTED_PROVIDER}/{EXPECTED_MODEL}(静默模型漂移复发)"
    )


def test_diagnosis_default_unchanged(tmp_path, monkeypatch):
    """诊断链本次不动 —— 防止有人"顺手"把整个函数默认全改掉,波及诊断成本与效果。"""
    _api_url, _api_key, model, provider = _get_config(tmp_path, monkeypatch, "geo_scoring", "diagnosis")
    assert provider == "dashscope", f"诊断默认 provider 被改成 {provider},本次改动不应波及诊断链"


def test_compose_mounts_settings_json_into_every_app_service():
    """部署层:四个应用容器(blue/green/cron-blue/cron-green)都必须挂 settings.json。

    只挂 web 不挂 cron 会造成更隐蔽的分裂:人工触发的写作对、定时任务写出来的错。
    """
    compose = io.open(REPO / "docker-compose.yml", encoding="utf-8").read()
    mounts = re.findall(r"^\s*-\s*\S*settings\.json:/app/settings\.json:ro\s*$", compose, re.M)
    assert len(mounts) == 4, (
        f"settings.json 挂载只出现 {len(mounts)} 次,应为 4 次"
        "(omnirank-blue / green / cron-blue / cron-green 各一)"
    )
    assert all("OMNIRANK_RUNTIME_ROOT" in m for m in mounts), (
        "挂载源必须走 OMNIRANK_RUNTIME_ROOT(常驻宿主机、跨 release 存活);"
        "写成 ./settings.json 会指向 release 目录,新 release 一上来又是空的"
    )


def test_settings_json_is_readonly_mount():
    """只读挂载:配置由运维改宿主机文件,容器不得回写(避免容器重建时改动凭空消失)。"""
    compose = io.open(REPO / "docker-compose.yml", encoding="utf-8").read()
    bad = re.findall(r"^\s*-\s*\S*settings\.json:/app/settings\.json(?!:ro)\S*\s*$", compose, re.M)
    assert not bad, f"存在非只读的 settings.json 挂载:{bad}"
